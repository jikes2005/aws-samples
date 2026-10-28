import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base "./" keeps asset URLs relative, so the built app works from any path.
// For local development, set VITE_API_BASE_URL (e.g. http://localhost:8000) in the shell.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: { outDir: "dist", sourcemap: false },
});
