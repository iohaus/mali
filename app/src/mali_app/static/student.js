let activeLessonSource = null;
let activeCurriculumSource = null;

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

function appendProcessingMessage(messages, text) {
  const article = document.createElement("article");
  const copy = document.createElement("p");
  article.className = "message tutor-message processing-message";
  copy.textContent = text;
  article.append(copy, processingDots());
  messages.append(article);
  messages.scrollTop = messages.scrollHeight;
  return article;
}

function processingDots() {
  const dots = document.createElement("span");
  dots.className = "processing-dots";
  dots.setAttribute("aria-hidden", "true");
  for (let index = 0; index < 3; index += 1) {
    dots.append(document.createElement("span"));
  }
  return dots;
}

function removeElement(element) {
  if (element) element.remove();
}

function startLesson(url) {
  const messages = document.querySelector("#lesson-messages");
  if (!messages || !url) return;
  if (activeLessonSource) activeLessonSource.close();
  const pending = appendProcessingMessage(messages, "Your tutor is thinking");
  const source = new EventSource(url);
  activeLessonSource = source;
  let active = null;
  source.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    if (payload.text) {
      removeElement(pending);
      if (!active) active = appendMessage(messages, "", "tutor-message");
      active.textContent += payload.text;
    }
    if (payload.outcome) {
      removeElement(pending);
      source.close();
      if (activeLessonSource === source) activeLessonSource = null;
    }
  };
  source.onerror = () => {
    removeElement(pending);
    source.close();
    if (activeLessonSource === source) activeLessonSource = null;
  };
}

function showCurriculumStatus(text, pending) {
  const status = document.querySelector("#curriculum-status");
  if (!status) return;
  status.hidden = false;
  status.replaceChildren(document.createTextNode(text));
  if (pending) status.append(processingDots());
}

function curriculumStepRow(step, index) {
  const row = document.createElement("li");
  const number = document.createElement("span");
  const body = document.createElement("div");
  const title = document.createElement("strong");
  const description = document.createElement("p");
  number.className = "path-number";
  number.textContent = String(index + 1);
  title.textContent = step.title;
  description.textContent = step.description;
  body.append(title, description);
  row.append(number, body);
  return row;
}

function revealCurriculum(payload) {
  const heading = document.querySelector("#curriculum-title");
  const summary = document.querySelector(".curriculum-summary");
  const steps = document.querySelector("#curriculum-steps");
  if (heading) heading.textContent = payload.title;
  if (summary) summary.textContent = payload.summary;
  if (!steps) return Promise.resolve();
  steps.replaceChildren();
  steps.hidden = false;
  const instant = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  return new Promise((resolve) => {
    payload.steps.forEach((step, index) => {
      const row = curriculumStepRow(step, index);
      if (instant) {
        steps.append(row);
        return;
      }
      row.classList.add("step-arriving");
      window.setTimeout(() => steps.append(row), 260 * index);
    });
    const settleDelay = instant ? 0 : 260 * payload.steps.length + 200;
    window.setTimeout(resolve, settleDelay);
  });
}

function startCurriculumBuild(url) {
  if (!url) return;
  if (activeCurriculumSource) activeCurriculumSource.close();
  showCurriculumStatus("Mali is designing your curriculum", true);
  const source = new EventSource(url);
  activeCurriculumSource = source;
  let reveal = Promise.resolve();
  source.addEventListener("status", (event) => {
    const payload = JSON.parse(event.data);
    showCurriculumStatus(payload.text, payload.state === "building");
  });
  source.addEventListener("curriculum", (event) => {
    const payload = JSON.parse(event.data);
    showCurriculumStatus("Here is your curriculum", false);
    reveal = revealCurriculum(payload);
  });
  source.addEventListener("outcome", (event) => {
    const payload = JSON.parse(event.data);
    source.close();
    if (activeCurriculumSource === source) activeCurriculumSource = null;
    if (payload.outcome !== "completed") return;
    reveal.then(() => {
      const status = document.querySelector("#curriculum-status");
      if (status) status.hidden = true;
      const start = document.querySelector("#curriculum-start");
      if (start) {
        start.hidden = false;
        start.focus();
      } else {
        window.location.reload();
      }
    });
  });
  source.onerror = () => {
    source.close();
    if (activeCurriculumSource === source) activeCurriculumSource = null;
  };
}

function connectQueuedLesson(root = document) {
  const queued = root.querySelector("[data-lesson-url]");
  if (!queued) return;
  const url = queued.getAttribute("data-lesson-url");
  queued.remove();
  startLesson(url);
}

document.addEventListener("DOMContentLoaded", () => connectQueuedLesson());
document.body.addEventListener("htmx:afterSwap", (event) => connectQueuedLesson(event.target));

document.addEventListener("click", (event) => {
  if (!(event.target instanceof HTMLElement)) return;
  if (event.target.id === "curriculum-start") window.location.reload();
});

document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return;
  if (form.id === "lesson-form") {
    event.preventDefault();
    const input = form.elements.namedItem("student_turn");
    if (!(input instanceof HTMLInputElement) || !input.value.trim()) return;
    const messages = document.querySelector("#lesson-messages");
    if (messages) appendMessage(messages, input.value.trim(), "student-message");
    const query = new URLSearchParams({ student_turn: input.value.trim() });
    input.value = "";
    startLesson(`${form.dataset.streamPath}?${query}`);
    return;
  }
  if (form.id !== "curriculum-form") return;
  event.preventDefault();
  const input = form.elements.namedItem("topic");
  if (!(input instanceof HTMLInputElement) || !input.value.trim()) return;
  const query = new URLSearchParams({ topic: input.value.trim() });
  input.value = "";
  startCurriculumBuild(`${form.dataset.streamPath}?${query}`);
});
