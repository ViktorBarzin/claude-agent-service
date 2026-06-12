import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

// The compiled SPA is emitted into the FastAPI app's static dir. FastAPI serves
// app/breakglass/static/index.html at "/" and mounts the directory, so the
// build output must be plain static files (plain Svelte, not SvelteKit).
//
// base: './' makes every asset reference relative, so the bundle loads no
// matter what path the edge proxy mounts the app under.
export default defineConfig({
  plugins: [svelte()],
  base: './',
  build: {
    outDir: '../app/breakglass/static',
    emptyOutDir: true,
    // Keep the asset graph small and predictable for an air-gapped cluster:
    // no remote chunks, no CDN — everything bundled here.
    assetsInlineLimit: 0,
  },
});
