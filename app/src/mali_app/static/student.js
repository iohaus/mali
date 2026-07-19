function appendMessage(messages, text, className) {
  const article = document.createElement("article");
  const paragraph = document.createElement("p");
  article.className = `message ${className}`;
  paragraph.textContent = text;
  article.append(paragraph);
  messages.append(article);
  messages.scrollTop = messages.scrollHeight;
  return paragraph;
}

function startLesson(url) {
  const messages = document.querySelector("#lesson-messages");
  if (!messages || !url) return;
  const source = new EventSource(url);
  let active = null;
  source.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    if (payload.text) {
      if (!active) active = appendMessage(messages, "", "tutor-message");
      active.textContent += payload.text;
    }
    if (payload.outcome) source.close();
  };
  source.onerror = () => source.close();
}

function connectQueuedLesson(root = document) {
  const queued = root.querySelector("[data-lesson-url]");
  if (!queued) return;
  const url = queued.dataset.lessonUrl;
  queued.removeAttribute("data-lesson-url");
  startLesson(url);
}

document.addEventListener("DOMContentLoaded", () => connectQueuedLesson());
document.body.addEventListener("htmx:afterSwap", (event) => connectQueuedLesson(event.target));

document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement) || form.id !== "lesson-form") return;
  event.preventDefault();
  const input = form.elements.namedItem("student_turn");
  if (!(input instanceof HTMLInputElement) || !input.value.trim()) return;
  const messages = document.querySelector("#lesson-messages");
  if (messages) appendMessage(messages, input.value.trim(), "student-message");
  const query = new URLSearchParams({ student_turn: input.value.trim() });
  input.value = "";
  startLesson(`${form.dataset.streamPath}?${query}`);
});
