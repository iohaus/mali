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

/**
 * Render `text` as sanitized markdown HTML into `container`.
 * Falls back to leaving textContent untouched when either library is absent
 * (e.g. CDN blocked, reduced-functionality environment).
 */
function renderMarkdown(container, text) {
  if (typeof marked === "undefined" || typeof DOMPurify === "undefined") return;
  container.innerHTML = DOMPurify.sanitize(marked.parse(text));
}

/**
 * Find every [data-markdown] node inside `root` and render its textContent
 * as markdown in-place.  Called after each full-page load and HTMX swap so
 * server-rendered tutor messages are also processed.
 */
function renderMarkdownNodes(root = document) {
  root.querySelectorAll("[data-markdown]").forEach((el) => {
    const text = el.textContent;
    el.removeAttribute("data-markdown");
    renderMarkdown(el, text);
  });
}

function startLesson(url) {
  const messages = document.querySelector("#lesson-messages");
  if (!messages || !url) return;
  if (activeLessonSource) activeLessonSource.close();
  const pending = appendProcessingMessage(messages, "Your tutor is thinking");
  const source = new EventSource(url);
  activeLessonSource = source;

  // The <article> that accumulates this episode's response.
  let activeArticle = null;
  // Raw plain-text buffer — streamed as textContent; rendered as markdown on close.
  let rawText = "";

  function finaliseStream() {
    removeElement(pending);
    if (activeArticle && rawText) {
      renderMarkdown(activeArticle, rawText);
      messages.scrollTop = messages.scrollHeight;
    }
  }

  source.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    if (payload.text) {
      removeElement(pending);
      if (!activeArticle) {
        activeArticle = document.createElement("article");
        activeArticle.className = "message tutor-message";
        // Temporary <p> for streaming plain text; replaced on close.
        const streamParagraph = document.createElement("p");
        activeArticle.append(streamParagraph);
        messages.append(activeArticle);
      }
      rawText += payload.text;
      // Display each chunk as plain text while the stream is live so the
      // reader sees progress without broken mid-stream markdown syntax.
      const streamParagraph = activeArticle.querySelector("p");
      if (streamParagraph) streamParagraph.textContent = rawText;
      messages.scrollTop = messages.scrollHeight;
    }
    if (payload.outcome) {
      finaliseStream();
      source.close();
      if (activeLessonSource === source) activeLessonSource = null;
    }
  };
  source.onerror = () => {
    finaliseStream();
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
  if (step.assumed) {
    const badge = document.createElement("span");
    badge.className = "assumed-badge";
    badge.textContent = "you may know this — checked first";
    title.append(" ", badge);
  }
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

document.addEventListener("DOMContentLoaded", () => {
  connectQueuedLesson();
  renderMarkdownNodes();
});
document.body.addEventListener("htmx:afterSwap", (event) => {
  connectQueuedLesson(event.target);
  renderMarkdownNodes(event.target);
});

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
