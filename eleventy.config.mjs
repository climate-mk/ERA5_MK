import SolidPlugin from "vite-plugin-solid";
import TailwindCSS from "@tailwindcss/vite";
import EleventyVitePlugin from "@11ty/eleventy-plugin-vite";

export default function (eleventyConfig) {
  eleventyConfig.addPlugin(EleventyVitePlugin, {
    viteOptions: {
      plugins: [TailwindCSS(), SolidPlugin()],
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
