const KIND_LABEL = {
  weakness: "薄弱点",
  progress: "进度",
  preference: "偏好",
  memory: "记忆",
};

const state = { learner: "", sessionId: "", questions: [] };
const $ = (selector) => document.querySelector(selector);

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || `请求失败（${response.status}）`);
  }
  return data;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function chip(kind, text, entryId, score) {
  const wrapper = el("span", "chip");
  const tag = el("span", `tag ${kind}`, KIND_LABEL[kind] || kind);
  wrapper.append(tag, el("span", "", text));
  if (score !== undefined) {
    wrapper.append(el("code", "", `score ${score}`));
  }
  if (entryId) wrapper.append(el("code", "", entryId.slice(0, 18)));
  return wrapper;
}

function setBadge(ok, text) {
  const badge = $("#pcBadge");
  badge.classList.remove("pending", "ok", "bad");
  badge.classList.add(ok ? "ok" : "bad");
  badge.textContent = text;
}

async function bootstrap(learner) {
  if (!learner) return null;
  try {
    const data = await api(`/api/bootstrap?learner=${encodeURIComponent(learner)}`);
    if (!data.pc_ok) {
      setBadge(false, "PowerContext 未连接");
      $("#setupHint").textContent =
        "未检测到本地 PowerContext Server。请先运行 powercontext server run，再刷新本页。";
      return null;
    }
    setBadge(true, `PowerContext 已连接`);
    $("#weakOnly").checked = data.prefers_weak_only;
    const weakTopics = Object.entries(data.weak_topics || {});
    const weakText = weakTopics.length
      ? `已记住 ${data.memory_count} 条记忆，薄弱点：${weakTopics
          .map(([topic, count]) => `${topic}×${count}`)
          .join("、")}`
      : "这名学习者还没有历史记忆，先完成一轮复习，系统会开始记住薄弱点。";
    $("#setupHint").textContent = weakText;
    return data;
  } catch (error) {
    setBadge(false, "连接失败");
    $("#setupHint").textContent = error.message;
    return null;
  }
}

function renderRecall(data) {
  const panel = $("#recallPanel");
  const list = $("#recallList");
  const weak = Object.entries(data.weak_topics || {});
  $("#scopeLabel").textContent = `scope：${state.scopeId || ""}`;
  list.replaceChildren();
  if (data.recalled.length === 0) {
    list.append(chip("memory", "这是第一次复习，PowerContext 里还没有可召回的记录。"));
  } else {
    data.recalled.forEach((item) => {
      list.append(chip(item.kind, item.text, item.entry_id, item.score));
    });
  }
  $("#weakHint").textContent = weak.length
    ? `本次排题依据：${weak.map(([topic, count]) => `${topic}×${count}`).join("、")}${
        data.weak_only ? "（只复习薄弱点）" : ""
      }`
    : data.weak_only
      ? "暂无可定位的历史薄弱点，本次按难度安排题目。"
      : "本轮没有历史薄弱点可用于加权，按难度安排题目。";
  panel.classList.remove("hidden");
}

function renderQuestions(questions) {
  const form = $("#quizForm");
  form.replaceChildren();
  $("#quizCount").textContent = `${questions.length} 题`;
  questions.forEach((question, index) => {
    const card = el("article", "qcard");
    const top = el("div", "qtop");
    top.append(
      el("span", "topic", question.topic),
      el("span", "difficulty", `难度 ${question.difficulty} · 第 ${index + 1} 题`)
    );
    card.append(top, el("p", "qtext", question.question));
    const options = el("div", "options");
    question.options.forEach((option, optionIndex) => {
      const label = el("label", "option");
      const input = el("input");
      input.type = "radio";
      input.name = `q_${question.id}`;
      input.value = String(optionIndex);
      input.addEventListener("change", () => {
        options.querySelectorAll(".option").forEach((node) => node.classList.remove("selected"));
        label.classList.add("selected");
      });
      label.append(input, el("span", "", option));
      options.append(label);
    });
    card.append(options);
    form.append(card);
  });
  $("#quizPanel").classList.remove("hidden");
  $("#submitBtn").classList.remove("hidden");
  $("#againBtn").classList.add("hidden");
}

function renderResult(data) {
  const panel = $("#resultPanel");
  const pct = data.total ? Math.round((data.score / data.total) * 100) : 0;
  $("#scoreText").textContent = `${data.score}/${data.total}`;
  panel.querySelector(".score-ring").style.setProperty("--pct", `${pct}%`);
  $("#resultSummary").textContent =
    pct === 100
      ? "全部答对。PowerContext 已更新本轮进度。"
      : "答错的知识点会作为薄弱点写入记忆，下一轮优先出现。";
  const details = $("#resultDetails");
  details.replaceChildren();
  const letter = (index) => (index === null || index === undefined ? "未作答" : String.fromCharCode(65 + index));
  data.details.forEach((item) => {
    const row = el("div", `detail ${item.correct ? "correct" : "wrong"}`);
    row.append(el("span", "verdict", item.correct ? "答对" : "答错"));
    row.append(el("span", "", item.question));
    row.append(
      el(
        "div",
        "explain",
        `${item.correct ? "" : `你选了 ${letter(item.selected)}；`}正确答案 ${letter(item.answer)}. ${
          item.options ? item.options[item.answer] : ""
        } ${item.explanation}`
      )
    );
    details.append(row);
  });
  const writes = $("#writeList");
  writes.replaceChildren();
  data.writes.forEach((write) => writes.append(chip(write.kind, write.text, write.entry_id)));
  $("#recallPanel").classList.add("hidden");
  panel.classList.remove("hidden");
  $("#quizPanel").classList.add("hidden");
  $("#againBtn").classList.remove("hidden");
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function startSession() {
  const learner = $("#learner").value.trim();
  if (!learner) {
    alert("请先填写学习者名称");
    return;
  }
  const button = $("#startBtn");
  button.disabled = true;
  try {
    const data = await api("/api/session/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ learner, weak_only: $("#weakOnly").checked }),
    });
    state.learner = data.learner;
    state.sessionId = data.session_id;
    state.questions = data.questions;
    state.scopeId = "";
    const boot = await bootstrap(learner);
    state.scopeId = boot ? boot.scope_id : "";
    renderRecall(data);
    renderQuestions(data.questions);
    $("#resultPanel").classList.add("hidden");
    $("#recallPanel").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
  }
}

async function submitAnswers() {
  const answers = state.questions.map((question) => {
    const selected = document.querySelector(`input[name="q_${question.id}"]:checked`);
    return selected ? { id: question.id, choice: Number(selected.value) } : null;
  });
  if (answers.some((answer) => answer === null)) {
    alert("还有题目没有作答");
    return;
  }
  const button = $("#submitBtn");
  button.disabled = true;
  try {
    const data = await api("/api/session/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        learner: state.learner,
        session_id: state.sessionId,
        answers,
      }),
    });
    renderResult(data);
    await bootstrap(state.learner);
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
  }
}

async function toggleMemory() {
  const list = $("#memoryList");
  if (!list.classList.contains("hidden")) {
    list.classList.add("hidden");
    $("#memoryBtn").textContent = "查看全部记忆";
    return;
  }
  try {
    const data = await api(`/api/memory?learner=${encodeURIComponent($("#learner").value.trim())}`);
    list.replaceChildren();
    if (data.entries.length === 0) {
      list.append(el("p", "hint", "还没有记忆，完成一轮复习后即可看到。"));
    }
    data.entries.forEach((entry) => {
      const item = el("div", "mem-item");
      item.append(el("span", `tag ${entry.kind}`, KIND_LABEL[entry.kind] || entry.kind));
      item.append(el("span", "", entry.text));
      item.append(el("code", "", entry.entry_id));
      list.append(item);
    });
    list.classList.remove("hidden");
    $("#memoryBtn").textContent = "收起";
  } catch (error) {
    alert(error.message);
  }
}

$("#startBtn").addEventListener("click", startSession);
$("#submitBtn").addEventListener("click", submitAnswers);
$("#againBtn").addEventListener("click", startSession);
$("#memoryBtn").addEventListener("click", toggleMemory);
$("#learner").addEventListener("blur", () => bootstrap($("#learner").value.trim()));

bootstrap($("#learner").value.trim());