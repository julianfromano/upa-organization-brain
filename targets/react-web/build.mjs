import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { copyFile, mkdir, readFile, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { build } from "esbuild";

const root = path.dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);

function option(name) {
  const index = args.indexOf(name);
  return index < 0 ? null : args[index + 1];
}

const output = option("--out");
const inputUi = option("--ui-ir");
if (!output || !inputUi) {
  throw new Error("Usage: npm run build -- --ui-ir <ui-ir.json> --out <output-directory>");
}
const callerRoot = process.env.INIT_CWD ?? process.cwd();
const outputPath = path.resolve(callerRoot, output);
const inputUiPath = path.resolve(callerRoot, inputUi);
const requireFromProvider = createRequire(path.join(root, "package.json"));
let uiSource;
try {
  uiSource = await readFile(inputUiPath, "utf8");
} catch (error) {
  throw new Error(`Unable to read UI IR at ${inputUiPath}: ${error.message}`, { cause: error });
}
const ui = JSON.parse(uiSource);
if (!ui.frontend_required) throw new Error("The Logical IR does not require a frontend; no bundle was generated.");

const catalog = JSON.parse(await readFile(path.join(root, "catalog.json"), "utf8"));
if (catalog.format !== "component-catalog-1" || catalog.provider !== "react-web" || catalog.platform !== "React DOM") {
  throw new Error("The component catalog must identify provider react-web on React DOM.");
}
const componentSourcePath = path.join(root, "src", "components.jsx");
const componentSource = await readFile(componentSourcePath, "utf8");
const roles = new Set(ui.interactions.flatMap((interaction) =>
  interaction.fields.map((field) => field.role)));
for (const role of ["primary-action", "feedback", "content-group", "heading"]) roles.add(role);
const resolvedRoles = {};
for (const role of [...roles].sort()) {
  const catalogEntry = catalog.roles[role];
  if (!catalogEntry) throw new Error(`Catalog ${catalog.version} has no component for role ${role}.`);
  const exports = Array.isArray(catalogEntry) ? catalogEntry : [catalogEntry];
  for (const exportName of exports) {
    if (!catalog.contracts[exportName]) {
      throw new Error(`Catalog ${catalog.version} has no contract for component ${exportName}.`);
    }
    if (!componentSource.includes(`export function ${exportName}(`)) {
      throw new Error(`Component ${exportName} is not exported by ${componentSourcePath}.`);
    }
  }
  resolvedRoles[role] = exports;
}

const safeName = (value) => String(value).replace(/[&<>"']/g, (character) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[character]));
const renderableUi = {
  format: "upa-renderable-ui-1",
  source_profile: ui.profile,
  provider: catalog.provider,
  catalog: { name: catalog.name, version: catalog.version },
  roles: resolvedRoles,
  pages: ui.pages,
  interactions: ui.interactions.map((interaction) => ({
    id: interaction.id,
    title: interaction.title,
    page_id: interaction.page_id,
    action_component_id: interaction.action_component_id,
    fields: interaction.fields.map((field) => ({
      component_id: field.component_id,
      component_kind: field.component_kind,
      label: field.label,
      role: field.role,
      required: field.required,
      min: field.min,
      input_type: field.input_type,
      ...(field.collection ? { collection: field.collection } : {}),
    })),
  })),
  collections: ui.collections.map((collection) => ({
    name: collection.name,
    source: collection.source,
    page_id: collection.page_id,
    fields: collection.fields.map((field) => ({ api_field: field.api_field, label: field.label })),
  })),
  ui_ir_sha256: createHash("sha256").update(uiSource, "utf8").digest("hex"),
};
await mkdir(outputPath, { recursive: true });
await writeFile(path.join(outputPath, "renderable-ui.json"), `${JSON.stringify(renderableUi, null, 2)}\n`, "utf8");
await writeFile(path.join(outputPath, "index.html"), `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="upa-ui-profile" content="${safeName(ui.profile.name)}">
  <title>${safeName(ui.profile.title ?? ui.pages[0]?.title ?? "UPA")}</title>
  <link rel="stylesheet" href="/app.css">
</head>
<body><div id="root" role="status">Loading application…</div><script type="module" src="/app.js"></script></body>
</html>
`, "utf8");
await copyFile(path.join(root, "src", "app.css"), path.join(outputPath, "app.css"));

await build({
  entryPoints: [path.join(root, "src", "main.jsx")],
  outfile: path.join(outputPath, "app.js"),
  bundle: true,
  minify: true,
  sourcemap: false,
  format: "esm",
  platform: "browser",
  target: ["es2020"],
  jsx: "automatic",
  jsxImportSource: "react",
  plugins: [{
    name: "react-web-provider",
    setup(buildApi) {
      buildApi.onResolve({ filter: /^react$/ }, () => ({ path: requireFromProvider.resolve("react") }));
      buildApi.onResolve({ filter: /^react\/jsx-runtime$/ }, () => ({ path: requireFromProvider.resolve("react/jsx-runtime") }));
      buildApi.onResolve({ filter: /^react-dom\/client$/ }, () => ({ path: requireFromProvider.resolve("react-dom/client") }));
    },
  }],
});
console.log(JSON.stringify({ output: outputPath, provider: catalog.provider, catalog: `${catalog.name}@${catalog.version}`, resolvedRoles }));
