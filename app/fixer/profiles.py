"""Per-repo fixer settings.

The fixer started with one enrolled repo, so its trigger label, trust rule,
agent, prompts, CI follow-through and bot account were single values. A second
repo with different answers needs them per repo. A :class:`RepoProfile` holds
them, and :func:`profile_for` is the one place that says which repo gets what.

``infra`` (and any repo without its own entry) keeps exactly the values the
fixer had before: the configured trigger label (``broken``), any collaborator
may trigger, the configured agent (``issue-responder``), the infra prompts,
Woodpecker follow-through, the configured bot ignored, and comments never
trigger.

``f1-stream`` follows the user-reports build contract
(f1-stream ``docs/design/2026-10-16-user-reports-build-contract.md``):

- ``approved`` dispatches, and only when Viktor applied it; his approval takes
  ``needs-approval`` off, whether the run starts now or waits for the drain;
- ``f1-feedback-fixer`` runs with the f1-stream prompts;
- the service does not follow CI: the agent watches the build and deploy itself,
  and the service only notices when the run ends;
- both bots (the fixer's own ``infra-agent`` and the backend's
  ``tv-feedback-bot``) are ignored;
- a reply comment from Viktor on an approved issue starts a continue turn.
"""
from collections.abc import Iterable
from dataclasses import dataclass

from app.afk.tracker import DEFAULT_TRUSTED_ASSOCIATIONS, Tracker
from app.fixer import prompts
from app.fixer.config import FixerConfig
from app.fixer.runstate import RunRecord

#: The service follows the pushed commit through Woodpecker (``ci.py``).
CI_WOODPECKER = "woodpecker"
#: The service does not follow CI; the run's end is the only thing it watches.
CI_NONE = "none"

PLAYBOOK_INFRA = "infra"
PLAYBOOK_F1 = "f1-stream"

F1_STREAM = "f1-stream"
F1_TRIGGER_LABEL = "approved"
F1_APPROVER = "viktor"
F1_AGENT = "f1-feedback-fixer"
F1_FEEDBACK_BOT = "tv-feedback-bot"
F1_NEEDS_APPROVAL = "needs-approval"


@dataclass(frozen=True)
class RepoProfile:
    """How the fixer treats one repo.

    ``trusted_actors`` of ``None`` means "the repo's collaborators and owner, as
    Forgejo reports them". ``agent`` of ``None`` means the service's configured
    agent (``FIXER_AGENT``), and is submitted without naming one, as before.
    ``cleared_on_approval`` lists labels a verified trigger takes off the issue.
    """

    trigger_label: str
    trusted_actors: frozenset[str] | None
    agent: str | None
    ignored_actors: frozenset[str]
    ci_mode: str
    comment_trigger: bool
    playbook: str
    cleared_on_approval: frozenset[str] = frozenset()


def profile_for(repo: str, cfg: FixerConfig) -> RepoProfile:
    """The settings for ``repo``. Repos without an entry get infra's."""
    if repo == F1_STREAM:
        return RepoProfile(
            trigger_label=F1_TRIGGER_LABEL,
            trusted_actors=frozenset({F1_APPROVER}),
            agent=F1_AGENT,
            ignored_actors=frozenset({cfg.bot_actor, F1_FEEDBACK_BOT}),
            ci_mode=CI_NONE,
            comment_trigger=True,
            playbook=PLAYBOOK_F1,
            cleared_on_approval=frozenset({F1_NEEDS_APPROVAL}),
        )
    return RepoProfile(
        trigger_label=cfg.trigger_label,
        trusted_actors=None,
        agent=None,
        ignored_actors=frozenset({cfg.bot_actor}),
        ci_mode=CI_WOODPECKER,
        comment_trigger=False,
        playbook=PLAYBOOK_INFRA,
    )


def clear_on_approval(client, profile: RepoProfile, repo: str, number: int,
                      labels: Iterable[str]) -> None:
    """Take the profile's ``cleared_on_approval`` labels off, if the issue has them."""
    for label in sorted(profile.cleared_on_approval & frozenset(labels)):
        client.remove_label(repo, number, label)


# --------------------------------------------------------------------------- #
# Prompts by playbook.
# --------------------------------------------------------------------------- #
def first_turn(profile: RepoProfile, *, owner: str, repo: str, number: int, title: str,
               issue_url: str) -> str:
    common = dict(owner=owner, repo=repo, number=number, title=title, issue_url=issue_url)
    if profile.playbook == PLAYBOOK_F1:
        return prompts.f1_first_turn(**common)
    return prompts.first_turn(**common, trigger_label=profile.trigger_label)


def redispatch_turn(profile: RepoProfile, *, owner: str, repo: str, number: int,
                    title: str, issue_url: str) -> str:
    common = dict(owner=owner, repo=repo, number=number, title=title, issue_url=issue_url)
    if profile.playbook == PLAYBOOK_F1:
        return prompts.f1_redispatch_turn(**common)
    return prompts.redispatch_turn(**common)


def fix_forward_turn(profile: RepoProfile, *, owner: str, repo: str, number: int,
                     title: str, issue_url: str, record: RunRecord) -> str:
    """Only a repo whose CI the service follows has a corrective turn."""
    if profile.ci_mode != CI_WOODPECKER:
        raise ValueError(f"{repo} has no fix-forward turn: the service does not follow its CI")
    return prompts.fix_forward_turn(owner=owner, repo=repo, number=number, title=title,
                                    issue_url=issue_url, record=record)


def continue_turn(profile: RepoProfile, *, owner: str, repo: str, number: int, title: str,
                  issue_url: str, reply: str) -> str:
    """The turn a trusted reply comment starts. Only comment-triggered repos have one."""
    if not profile.comment_trigger:
        raise ValueError(f"{repo} is not triggered by comments")
    return prompts.f1_continue_turn(owner=owner, repo=repo, number=number, title=title,
                                    issue_url=issue_url, reply=reply)


# --------------------------------------------------------------------------- #
# The drain's tracker.
# --------------------------------------------------------------------------- #
class RepoTrackers:
    """One ``Tracker`` per repo, each reading that repo's trigger label and trust.

    The drain lists ready work across every enrolled repo through one tracker,
    and ``Tracker`` speaks one ready label and one trust rule. This keeps one per
    repo and routes by repo, so infra is read exactly as before.

    Forgejo's timeline carries no ``author_association``, so ``ForgejoClient``
    synthesises it: ``OWNER`` for the repo owner, ``COLLABORATOR`` for the rest
    of the trusted set. A trusted set of just the owner is therefore "only
    ``OWNER`` counts"; any other fixed set cannot be expressed that way and is
    refused rather than widened.
    """

    def __init__(self, client, cfg: FixerConfig) -> None:
        self._client = client
        self._cfg = cfg
        self._trackers: dict[str, Tracker] = {}

    def _for(self, repo: str) -> Tracker:
        if repo not in self._trackers:
            profile = profile_for(repo, self._cfg)
            if profile.trusted_actors is None:
                associations = DEFAULT_TRUSTED_ASSOCIATIONS
            elif profile.trusted_actors == frozenset({self._cfg.owner}):
                associations = frozenset({"OWNER"})
            else:
                raise ValueError(f"cannot express trust {sorted(profile.trusted_actors)} "
                                 f"for {repo} as an author association")
            self._trackers[repo] = Tracker(self._client, ready_label=profile.trigger_label,
                                           trusted_associations=associations)
        return self._trackers[repo]

    def list_ready(self, repos):
        return [issue for repo in repos for issue in self._for(repo).list_ready([repo])]

    def list_in_progress(self, repos, label):
        return [issue for repo in repos
                for issue in self._for(repo).list_in_progress([repo], label)]

    def _to_issue(self, repo, raw):
        return self._for(repo)._to_issue(repo, raw)

    def add_label(self, repo, issue, label):
        self._client.add_label(repo, issue, label)

    def remove_label(self, repo, issue, label):
        self._client.remove_label(repo, issue, label)

    def comment(self, repo, issue, body):
        self._client.comment(repo, issue, body)

    def close(self, repo, issue):
        self._client.close(repo, issue)
