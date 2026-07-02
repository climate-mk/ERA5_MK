import path from "node:path";
import { fileURLToPath } from "node:url";
import SolidPlugin from "vite-plugin-solid";
import TailwindCSS from "@tailwindcss/vite";
import EleventyVitePlugin from "@11ty/eleventy-plugin-vite";
import { proxyConfig } from "./dev-proxy.mjs";

const __dirname = fileURLToPath(new URL(".", import.meta.url));

export default function (eleventyConfig) {
  eleventyConfig.addPlugin(EleventyVitePlugin, {
    viteOptions: {
      plugins: [TailwindCSS(), SolidPlugin()],
      server: { proxy: proxyConfig },
      resolve: {
        alias: {
          "/code":   path.resolve(__dirname, "code"),
          "/pages":  path.resolve(__dirname, "pages"),
          "/styles": path.resolve(__dirname, "styles"),
        },
      },
      define: {
        // In dev the browser uses proxy paths; in prod these are replaced at build time
        "import.meta.env.VITE_SIDECAR_URL":   JSON.stringify(""),
        "import.meta.env.VITE_DATASETTE_URL":  JSON.stringify("/datasette"),
      },
    },
  });

  eleventyConfig.addPassthroughCopy("assets");
  eleventyConfig.addPassthroughCopy({ "static/locales": "locales" });

  return {
    dir: {
      input: "pages",
      output: "dist",
    },
    htmlTemplateEngine: "liquid",
    markdownTemplateEngine: "liquid",
  };
}
