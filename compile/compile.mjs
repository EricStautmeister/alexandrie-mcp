// Reads a JSON array of Markdown strings on stdin, writes a JSON array of HTML strings.
// Uses the exact compiler bundled with the Alexandrie frontend (markdown.mjs, copied from
// the frontend image) so content_compiled matches what the UI produces.
import { c as compile } from "./markdown.mjs";

let input = "";
process.stdin.setEncoding("utf8");
for await (const chunk of process.stdin) input += chunk;
const docs = JSON.parse(input);
process.stdout.write(JSON.stringify(docs.map((md) => compile(md ?? ""))));
