// SPDX-FileCopyrightText: Milan Pikula
// SPDX-License-Identifier: EUPL-1.2

import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { compile } from "@vue/compiler-dom";

function compileInlineVueTemplates() {
  return {
    name: "mailintel-inline-template-compiler",
    enforce: "pre",
    transform(code, id) {
      if (!["/src/main.js", "/src/google-oauth-setup.js", "/src/message-addresses.js"].some(path => id.endsWith(path))) return null;

      const marker = "template: `";
      let cursor = 0;
      let last = 0;
      let output = "";
      let compiledCount = 0;

      while (cursor < code.length) {
        const markerIndex = code.indexOf(marker, cursor);
        if (markerIndex === -1) break;

        const templateStart = markerIndex + marker.length;
        let templateEnd = templateStart;
        while (templateEnd < code.length) {
          if (code[templateEnd] === "`" && code[templateEnd - 1] !== "\\") break;
          templateEnd += 1;
        }
        if (templateEnd >= code.length) {
          this.error(`Unterminated Vue template literal after offset ${markerIndex}`);
        }

        const template = code.slice(templateStart, templateEnd);
        const compiled = compile(template, {
          mode: "function",
          prefixIdentifiers: true,
        }).code;

        output += code.slice(last, markerIndex);
        output += `render: (() => {\n${compiled}\n})()`;
        last = templateEnd + 1;
        cursor = templateEnd + 1;
        compiledCount += 1;
      }

      if (!compiledCount) return null;
      output += code.slice(last);
      if (!output.includes('import * as Vue from "vue"')) {
        output = `import * as Vue from "vue";\n${output}`;
      }
      return { code: output, map: null };
    },
  };
}

export default defineConfig({
  plugins: [compileInlineVueTemplates(), vue()],
  build: {
    outDir: "../backend/mailintel/web/build",
    emptyOutDir: true,
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
});
