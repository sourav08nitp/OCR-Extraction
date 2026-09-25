const send = cmd => chrome.runtime.sendMessage(cmd).then(show);

function show(s) {
  if (!s) return;
  document.getElementById("status").textContent = s.message || "";
  const total = s.queue.length, pos = Math.min(s.pos, total);
  document.querySelector("#bar div").style.width = total ? (100 * pos / total) + "%" : "0";
  const saved = Object.keys(s.done || {}).length;
  document.getElementById("count").textContent =
    (total ? `This run: ${pos} of ${total} chapters. ` : "") + `Downloaded so far (all runs): ${saved}.`;
}

document.getElementById("start").onclick = () => {
  const books = [...document.querySelectorAll("input[type=checkbox]:checked")].map(i => i.value);
  if (!books.length) return show({ message: "Pick at least one book.", queue: [], pos: 0, done: {} });
  send({ cmd: "start", books });
};
document.getElementById("pause").onclick = () => send({ cmd: "pause" });
document.getElementById("resume").onclick = () => send({ cmd: "resume" });
document.getElementById("reset").onclick = () => {
  if (confirm("Forget which chapters were already downloaded? (Files on disk are not touched.)")) send({ cmd: "reset" });
};

chrome.storage.local.get("state").then(({ state }) => show(state || { message: "Ready.", queue: [], pos: 0, done: {} }));
chrome.storage.onChanged.addListener(ch => { if (ch.state) show(ch.state.newValue); });
