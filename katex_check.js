// Reads a JSON array of LaTeX strings on stdin, prints a JSON array: null if it renders, else the error message.
// Uses the same KaTeX version and options as static/index.html, so "valid" here means it displays there.
const katex = require("katex");

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (c) => (input += c));
process.stdin.on("end", () => {
  const out = JSON.parse(input).map((tex) => {
    try {
      katex.renderToString(tex, { throwOnError: true, displayMode: false, strict: "ignore" });
      return null;
    } catch (e) {
      return String(e.message || e).slice(0, 200);
    }
  });
  process.stdout.write(JSON.stringify(out));
});
