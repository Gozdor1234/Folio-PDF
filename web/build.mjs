// Builds dist/FolioPDF.html: one self-contained file (app + PDF.js + worker + pdf-lib + CSS + icon).
import { build } from "esbuild";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";

const VERSION = "0.2.0";
mkdirSync("dist", { recursive: true });

// Chromium refuses module workers from blob: URLs on file:// pages (opaque origin), so the
// PDF.js worker (shipped as an ES module) is rebundled as a classic script.
const workerBuild = await build({
  entryPoints: ["node_modules/pdfjs-dist/legacy/build/pdf.worker.mjs"],
  bundle: true,
  format: "iife",
  minify: true,
  target: ["chrome110"],
  write: false,
  legalComments: "none",
  logLevel: "warning",
});
const worker = workerBuild.outputFiles[0].text;
writeFileSync("src/worker-src.js", "export default " + JSON.stringify(worker) + ";\n");

const res = await build({
  entryPoints: ["src/main.js"],
  bundle: true,
  format: "esm",
  minify: true,
  target: ["chrome110", "edge110"],
  write: false,
  legalComments: "none",
  logLevel: "warning",
});
let js = res.outputFiles[0].text;
// an inline <script> ends at the first "</script" no matter where it appears
const bad = [...js.matchAll(/<\/script|<!--|<script/gi)].map((m) => m[0]);
if (bad.length) console.log("escaping inline-script hazards:", bad);
js = js.replace(/<\/(script)/gi, "<\\/$1");
if (/<!--/.test(js) && /<script/i.test(js)) throw new Error("bundle contains <!-- and <script; cannot inline safely");

const css = readFileSync("src/style.css", "utf8") + "\n" + readFileSync("src_textlayer.css", "utf8");
const icon = "data:image/png;base64," + readFileSync("../folio-pdf/assets/folio.png").toString("base64");
const html = readFileSync("src/index.html", "utf8")
  .replace("__CSS__", () => css)
  .replace("__ICON__", () => icon)
  .replace("__VERSION__", () => VERSION)
  .replace("__JS__", () => js);
writeFileSync("dist/FolioPDF.html", html);
console.log("dist/FolioPDF.html", (html.length / 1024 / 1024).toFixed(2), "MB");
