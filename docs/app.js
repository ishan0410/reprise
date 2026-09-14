(function () {
  "use strict";
  const D = window.REPRISE;
  if (!D) return;

  const GH = "https://github.com/ishan0410/reprise/blob/main/";
  const GH_TREE = "https://github.com/ishan0410/reprise/tree/main/";
  const $ = (sel) => document.querySelector(sel);
  const el = (tag, attrs, children) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k === "html") n.innerHTML = v;
      else n.setAttribute(k, v);
    }
    for (const c of [].concat(children || [])) if (c != null) n.append(c);
    return n;
  };
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const bind = (key, value) => document.querySelectorAll(`[data-bind="${key}"]`).forEach((n) => (n.textContent = value));
  const bindHref = (key, href) => document.querySelectorAll(`[data-bind-href="${key}"]`).forEach((n) => n.setAttribute("href", href));
  const ms = (t) => new Date(t).getTime();
  const fmtMs = (v) => (v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${v} ms`);
  const fmtTime = (iso) => iso.replace("T", " ").replace(/\.\d+\+00:00$/, " UTC");

  // JSON syntax highlighting over escaped text; optional line highlighting.
  function jsonHtml(obj) {
    return esc(JSON.stringify(obj, null, 2)).replace(
      /(&quot;(?:[^&]|&(?!quot;))*?&quot;)(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:e[+-]?\d+)?)/g,
      (m, str, colon, lit, num) => {
        if (str) return colon ? `<span class="j-key">${str}</span>${colon}` : `<span class="j-str">${str}</span>`;
        if (lit) return `<span class="j-lit">${lit}</span>`;
        return `<span class="j-num">${num}</span>`;
      }
    );
  }
  function highlightLines(html, predicate) {
    return html
      .split("\n")
      .map((line) => (predicate(line) ? `<span class="hl">${line}</span>` : line))
      .join("\n");
  }

  function tabs(container, items, onSelect) {
    const root = $(container);
    const buttons = items.map((it, i) => {
      const b = el("button", { class: "tab", role: "tab", type: "button", "aria-selected": i === 0 ? "true" : "false", text: it.label });
      b.addEventListener("click", () => select(i));
      root.append(b);
      return b;
    });
    function select(i) {
      buttons.forEach((b, j) => b.setAttribute("aria-selected", j === i ? "true" : "false"));
      onSelect(items[i], i);
    }
    select(0);
  }

  function zoomable(shotSel) {
    const shot = $(shotSel);
    shot.querySelector("img").addEventListener("click", () => shot.classList.toggle("full"));
  }

  const disc = D.discovery;
  const art = D.artifact.json;
  const rs = D.replays.success.result;
  const E = D.escalation;
  const discWall = ms(disc.finished_at) - ms(disc.started_at);
  bind("disc-goal", disc.goal);

  /* ---------------- hero race: same task, with and without the model ---------------- */
  (function race() {
    const SPEED = 4;
    const reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const dLog = disc.run_log;
    const d0 = ms(dLog.find((e) => e.kind === "discovery.start").ts);
    const total = ms(dLog.find((e) => e.kind === "discovery.end").ts) - d0;
    const dSteps = dLog.filter((e) => e.kind === "step").map((e) => ({ at: ms(e.ts) - d0, tool: e.tool, detail: e.detail }));
    const rLog = D.replays.success.run_log;
    const r0 = ms(rLog.find((e) => e.kind === "replay.start").ts);
    const rSteps = rLog.filter((e) => e.kind === "step").map((e) => ({ at: ms(e.ts) - r0, step: e.step, action: e.action, dur: e.duration_ms }));
    const rEnd = rs.duration_ms;
    const tokens = disc.steps.reduce((n, s) => n + s.usage.input_tokens + s.usage.output_tokens, 0);
    const pct = (v) => `${Math.min(100, (v / total) * 100)}%`;
    const secs = (v, digits) => `${(v / 1000).toFixed(digits)} s`;

    bind("race-speed", SPEED);
    bind("race-model", disc.steps[0].model);

    const mkTicks = (trackSel, items, tip) =>
      items.map((it) => {
        const t = el("span", { class: "tick", tabindex: "0", "data-tip": tip(it) });
        t.style.left = pct(it.at);
        if (it.at / total > 0.6) t.classList.add("tip-end");
        else if (it.at / total < 0.3) t.classList.add("tip-start");
        $(trackSel).append(t);
        return t;
      });
    const dTicks = mkTicks("#track-disc", dSteps, (s) => `${secs(s.at, 1)} · ${s.tool}: ${s.detail.length > 48 ? s.detail.slice(0, 48) + "…" : s.detail}`);
    const rTicks = mkTicks("#track-replay", rSteps, (s) => `${s.at} ms · ${s.step} ${s.action} (${s.dur} ms)`);
    const axis = $("#race-axis");
    [0, 5000, 10000, 15000].filter((v) => v < total - 1500).concat([total]).forEach((v) => {
      const lab = el("span", { text: v === total ? secs(v, 1) : `${v / 1000} s` });
      lab.style.left = pct(v);
      axis.append(lab);
    });

    disc.steps.forEach((s) => { const i = new Image(); i.src = s.screenshot; });
    const img = $("#race-img");
    let shown = -1;

    function render(sim) {
      sim = Math.min(sim, total);
      const done = dSteps.filter((s) => s.at <= sim).length;
      $("#track-disc .fill").style.width = pct(sim);
      $("#clock-disc").textContent = secs(sim, 1);
      dTicks.forEach((t, i) => t.classList.toggle("on", dSteps[i].at <= sim));

      const rSim = Math.min(sim, rEnd);
      $("#track-replay .fill").style.width = pct(rSim);
      $("#clock-replay").textContent = secs(rSim, 2);
      rTicks.forEach((t, i) => t.classList.toggle("on", rSteps[i].at <= sim));

      const idx = Math.min(done, disc.steps.length - 1);
      if (idx !== shown) {
        shown = idx;
        img.src = disc.steps[idx].screenshot;
        $("#race-url").textContent = disc.steps[idx].url.replace(/^https?:\/\//, "");
      }
      $("#race-action").textContent = done ? `✓ ${dSteps[done - 1].detail}` : "model is reading the login page…";

      $("#stat-replay").innerHTML = sim >= rEnd ? `<b>done</b> · ${rSteps.length} browser steps · 0 model calls · 0 tokens` : "&nbsp;";
      $("#stat-disc").innerHTML = sim >= total ? `<b>done</b> · ${disc.steps.length} model calls · ${tokens.toLocaleString()} tokens` : `${done} of ${dSteps.length} model steps`;
      $("#race-callout").innerHTML =
        sim >= total ? `Same flow: <b>${secs(rEnd, 2)}</b> without a model, ${secs(total, 1)} with one.`
        : sim >= rEnd ? `Replay has already finished. Discovery is on step ${Math.min(done + 1, dSteps.length)} of ${dSteps.length}.`
        : "&nbsp;";
    }

    let raf = 0;
    function play() {
      cancelAnimationFrame(raf);
      if (reduced) return render(total);
      const start = performance.now();
      const frame = (now) => {
        const sim = (now - start) * SPEED;
        render(sim);
        if (sim < total) raf = requestAnimationFrame(frame);
      };
      raf = requestAnimationFrame(frame);
    }
    $("#race-play").addEventListener("click", play);
    render(0);
    if (reduced || !("IntersectionObserver" in window)) {
      play();
    } else {
      const io = new IntersectionObserver((entries) => {
        if (entries.some((e) => e.isIntersecting)) { io.disconnect(); play(); }
      }, { threshold: 0.35 });
      io.observe($("#race"));
    }
  })();

  /* ---------------- at a glance ---------------- */
  const gSuccess = $("#g-success");
  gSuccess.src = disc.steps[disc.steps.length - 1].screenshot;
  gSuccess.title = "Member Detail page, captured during discovery (successful replays don't take screenshots)";
  $("#g-outcome").src = D.replays.business_outcome.screenshots[0];
  $("#g-failure").src = D.replays.failure.screenshots[0];
  $("#g-escalation").src = E.screenshots[0];
  {
    const ev = E.result.control_events;
    const ceded = ev.find((x) => x.event === "ceded");
    const resumed = ev.find((x) => x.event === "resumed");
    const held = Math.round((ms(resumed.at) - ms(ceded.at)) / 1000);
    $("#g-held").textContent = `· held ${Math.floor(held / 60)}m ${held % 60}s`;
  }

  /* ---------------- 01 discovery ---------------- */
  bind("disc-path", disc.path);
  bindHref("disc-path", GH_TREE + disc.path);
  const stepper = $("#disc-steps");
  const stepButtons = disc.steps.map((s, i) => {
    const call = s.tool_calls[0] || { name: "?" };
    const b = el("button", { class: "step-btn", role: "tab", type: "button", "aria-selected": "false", "aria-label": `Step ${s.index}: ${call.name}` }, [
      String(s.index),
      el("span", { class: "tool", text: call.name }),
    ]);
    b.addEventListener("click", () => showStep(i));
    stepper.append(b);
    return b;
  });
  const arrows = el("div", { class: "nav-arrows" });
  const prev = el("button", { class: "tab", type: "button", text: "← prev" });
  const next = el("button", { class: "tab", type: "button", text: "next →" });
  arrows.append(prev, next);
  stepper.append(arrows);
  let cur = 0;
  prev.addEventListener("click", () => showStep(Math.max(0, cur - 1)));
  next.addEventListener("click", () => showStep(Math.min(disc.steps.length - 1, cur + 1)));
  zoomable("#disc-shot");

  function showStep(i) {
    cur = i;
    const s = disc.steps[i];
    stepButtons.forEach((b, j) => b.setAttribute("aria-selected", j === i ? "true" : "false"));
    const img = $("#disc-img");
    img.src = s.screenshot;
    img.alt = `Screenshot before step ${s.index}: ${s.title}`;
    $("#disc-shot-cap").textContent = `step-${String(s.index).padStart(2, "0")}.png · ${s.title}`;
    $("#disc-title").textContent = s.title;
    $("#disc-url").textContent = s.url;
    $("#disc-shot-sent").textContent = s.screenshot_sent_to_model ? "+ screenshot sent to model" : "text only";

    const call = s.tool_calls[0] || {};
    const ref = call.arguments && call.arguments.ref;
    const snap = $("#disc-snapshot");
    snap.innerHTML = highlightLines(esc(s.snapshot), (line) => ref && line.includes(`[ref=${ref}]`));
    const hl = snap.querySelector(".hl");
    snap.scrollTop = hl ? Math.max(0, hl.offsetTop - snap.clientHeight / 2) : 0;
    const refLine = ref ? s.snapshot.split("\n").find((l) => l.includes(`[ref=${ref}]`)) : null;
    const indent = refLine ? refLine.length - refLine.trimStart().length : 0;
    snap.scrollLeft = Math.max(0, (indent - 4) * 7.5);

    $("#disc-call").innerHTML = jsonHtml(s.tool_calls);
    $("#disc-model").textContent = `${s.provider} / ${s.model}`;
    $("#disc-usage").textContent = `${s.usage.input_tokens.toLocaleString()} input tokens · ${s.usage.output_tokens} output tokens · ${fmtMs(s.latency_ms)} model latency` + (ref ? ` · ref ${ref} is highlighted in the snapshot` : "");

    const d = s.decision;
    $("#disc-decision").innerHTML = d
      ? `<span class="pill ${d.allowed ? "ok" : "bad"}">${d.allowed ? "allowed" : "refused"}</span> <span class="pill neutral">risk: ${esc(d.risk)}</span> <code>${esc(d.reason)}</code>`
      : `<span class="pill neutral">terminal tool, no browser action</span>`;
    $("#disc-result").innerHTML = `<span class="pill ${s.result.status === "ok" || s.result.status === "done" ? "ok" : "bad"}">${esc(s.result.status)}</span> <code>${esc(s.result.detail)}</code><div class="small muted" style="margin-top:4px">url after: <code>${esc(s.url_after)}</code></div>`;
  }
  showStep(0);

  const d2 = D.discovery_2;
  const a2 = D.artifact_2.json;
  bind("disc2-path", d2.path);
  bindHref("disc2-path", GH_TREE + d2.path);
  const tools2 = d2.steps.map((s) => (s.tool_calls[0] || {}).name).join(" → ");
  $("#disc2-summary").innerHTML =
    `<code>open_sub_account_review</code>: ${d2.steps.length} model steps (${esc(tools2)}), compiled to a ${a2.steps.length}-step artifact that ends on the review screen. ` +
    `The model never tried the irreversible confirm, and the artifact's allowlist doesn't include a confirm path.`;

  /* ---------------- 02 artifact ---------------- */
  bindHref("art-path", GH + D.artifact.path);
  const step = (id) => art.steps.find((x) => x.id === id);
  const pick = (o, keys) => Object.fromEntries(keys.map((k) => [k, o[k]]));
  tabs("#art-tabs", [
    { label: "Identity & I/O", cap: "identity, target, inputs, outputs", obj: pick(art, ["schema_version", "id", "name", "version", "status", "description", "target", "inputs", "outputs"]),
      note: "The literal member ID from the goal became {{input:member_id}}. The balance output is marked sensitive, so it is redacted in every log and result file. Step descriptions are the model's one-line reasons, kept for reviewers; replay doesn't use them." },
    { label: "s4 · locator chain", cap: "steps[s4]: click with ranked candidates + checkpoint", obj: step("s4"),
      note: "Four candidates, from most to least robust. The recorded block is for reviewers and drift diagnosis and is never used to resolve the element. The checkpoint was derived from the page the model actually reached." },
    { label: "s8 · extract", cap: "steps[s8]: value-independent extract locator", obj: step("s8"),
      note: "The 4th cell of the row containing a cell named exactly 'Savings'. It doesn't depend on the balance text, which changes from run to run." },
    { label: "Conditions", cap: "success, outcomes, recoverables, failures", obj: pick(art, ["success", "outcomes", "recoverables", "failures"]),
      note: "Merged in from policies/mock-portal.conditions.json, a reviewed per-app list. Handling a new message is a JSON edit." },
    { label: "Policy & provenance", cap: "policy, provenance", obj: pick(art, ["policy", "overrides", "provenance"]),
      note: "Least privilege comes from the run itself: only the one origin, the four path patterns and the four action types it used. No select, no press, no /__admin. Provenance points to the discovery evidence and records the review that moved the artifact to approved." },
  ], (it) => {
    $("#art-cap").textContent = it.cap;
    $("#art-code").innerHTML = jsonHtml(it.obj);
    $("#art-code").scrollTop = 0;
    $("#art-note").textContent = it.note;
  });

  /* ---------------- 03 replay ---------------- */
  bind("disc-member", disc.inputs.member_id);
  bind("flow-art-path", D.artifact.path);
  bind("replay-member", rs.inputs.member_id);
  bind("disc-model-name", disc.steps[0].model);
  bind("disc-wall", fmtMs(discWall));
  bind("disc-wall-note", `${disc.steps.length} model calls, ${disc.total_input_tokens.toLocaleString()} input tokens, one step at a time (discovery.start → discovery.end).`);
  bind("replay-ms-2", fmtMs(rs.duration_ms));
  bind("replay-note", `${rs.steps.length} steps for member ${rs.inputs.member_id}, 0 model calls, 0 tokens (result.json duration_ms).`);

  const replayRuns = [
    { key: "success", label: "Plain replay", desc: "Different input from the recording (member 10003 instead of 10001). Every step resolved on its first candidate, so the drift report is empty." },
    { key: "recovery", label: "Session expiry recovered", desc: "A session timeout was injected through the mock app's test fixture. It surfaced at the s4 checkpoint, the engine re-ran the bootstrap phase, and the run completed." },
    { key: "second_capability", label: "Multi-field capability", desc: "open_sub_account_review replayed with different inputs than it was recorded with (Savings / Travel / 40). It stops on the review page without confirming." },
    { key: "catalog_invoke", label: "Agent tool call", desc: "The same capability invoked as a function-calling tool through cua-catalog invoke, after it was approved. An earlier invoke of the draft artifact was refused with exit 4 (per evidence/README.md)." },
  ];
  tabs("#replay-tabs", replayRuns.map((r) => ({ ...r, run: D.replays[r.key] })), (it) => {
    const res = it.run.result;
    $("#replay-desc").innerHTML = `<span class="pill ${res.status === "success" ? "ok" : "bad"}">${esc(res.status)}</span> <span class="pill neutral">exit ${it.run.exit_code}</span> <span class="pill neutral">${fmtMs(res.duration_ms)}</span> ${esc(it.desc)}`;
    const t = $("#replay-table");
    t.innerHTML = "";
    t.append(el("thead", {}, el("tr", {}, ["step", "action", "locator that resolved", "cand.", "time", "status"].map((h) => el("th", { text: h })))));
    const tb = el("tbody");
    res.steps.forEach((s) => {
      const cls = s.detail === "RestartPhase" ? "restart" : s.status === "failed" ? "failed" : s.status === "skipped" ? "skipped" : "";
      tb.append(el("tr", { class: cls }, [
        el("td", { class: "mono", html: `${esc(s.step_id)} <span class="phase-tag">${esc(s.phase)}</span>` }),
        el("td", { class: "mono", text: s.action }),
        el("td", { class: "mono", text: s.locator || "—" }),
        el("td", { class: "num", text: s.candidate_index == null ? "—" : String(s.candidate_index) }),
        el("td", { class: "num", text: `${s.duration_ms} ms` }),
        el("td", { html: `<span class="pill ${s.status === "ok" ? "ok" : s.status === "failed" ? "bad" : "neutral"}">${esc(s.status)}</span>${s.detail ? ` <code>${esc(s.detail)}</code>` : ""}` }),
      ]));
    });
    t.append(tb);
    const extra = $("#replay-extra");
    extra.innerHTML = "";
    const show = { outputs: res.outputs, recoveries: res.recoveries, drift: res.drift };
    extra.append(el("div", { class: "code-cap" }, [el("b", { text: "result.json: outputs, recoveries, drift" })]));
    extra.append(el("pre", { class: "code", html: jsonHtml(show) }));
    const link = $("#replay-link");
    link.href = GH_TREE + it.run.path;
    link.textContent = it.run.path;
  });

  /* ---------------- 04 results ---------------- */
  const cards = [
    { key: "success", cls: "ok", title: "Success", blurb: "The success condition held and the required outputs were read." , block: (r) => ({ status: r.status, outputs: r.outputs }) },
    { key: "business_outcome", cls: "warn", title: "Business outcome", blurb: "The application gave a legitimate non-happy answer. The caller gets an outcome code and the outputs that outcome sets.", block: (r) => ({ status: r.status, outputs: r.outputs, outcome: r.outcome }) },
    { key: "failure", cls: "bad", title: "Failure", blurb: "The flow couldn't complete. The error is precise: code, category, step, expected vs observed, whether a retry could help, and a screenshot.", block: (r) => ({ status: r.status, error: r.error }) },
  ];
  const oc = $("#outcome-cards");
  cards.forEach((c) => {
    const run = D.replays[c.key];
    const r = run.result;
    const card = el("div", { class: `card outcome-card ${c.cls}` });
    card.append(el("div", { class: "head" }, [
      el("h3", { text: c.title, style: "margin:0" }),
      el("span", { class: "exit", text: `member_id=${r.inputs.member_id} · exit ${run.exit_code}` }),
    ]));
    card.append(el("p", { text: c.blurb }));
    if (run.screenshots.length) {
      const shot = el("div", { class: "shot" }, el("img", { src: run.screenshots[0], alt: `${c.title} screenshot`, loading: "lazy" }));
      shot.querySelector("img").addEventListener("click", () => shot.classList.toggle("full"));
      card.append(shot);
    } else {
      card.append(el("p", { class: "small muted", text: "No screenshot: replay captures images on failure only by default." }));
    }
    card.append(el("pre", { class: "code", html: jsonHtml(c.block(r)) }));
    card.append(el("p", { class: "src-link", html: `<a href="${GH_TREE + run.path}">${esc(run.path)}</a>` }));
    oc.append(card);
  });

  /* ---------------- 05 safety ---------------- */
  const t0 = disc.steps[0];
  const t5 = disc.steps.find((s) => (s.tool_calls[0] || {}).name === "extract");
  const snapLines = (s, re) => s.snapshot.split("\n").filter((l) => re.test(l)).join("\n");
  tabs("#safety-tabs", [
    { label: "Gate decision", cap: "trace.json → steps[0]: the tool call, the intent, the decision",
      src: disc.path + "/trace.json",
      text: jsonHtml({ tool_calls: t0.tool_calls, decision: t0.decision }),
      note: "The model proposed typing a secret reference. The gate checked the action type, the allowlist and the secret name before anything happened." },
    { label: "Redaction", cap: "What the model and the logs saw on the Member Detail page",
      src: disc.path + "/transcript.jsonl",
      text: `<span class="dim">accessibility snapshot lines (verbatim):</span>\n` + highlightLines(esc(snapLines(t5, /Savings|REDACTED/)), (l) => l.includes("REDACTED")) + "\n\n" + jsonHtml({ tool_calls: t5.tool_calls, result: t5.result }),
      note: "The model marked the output sensitive when it extracted it. The value was added to the redactor before that step's transcript entry was written, so the balance appears nowhere in text evidence." },
    { label: "Environment policy", cap: "policies/mock-portal.toml", src: "policies/mock-portal.toml", text: esc(D.policy_toml),
      note: "Explicit denies win: the fault-injection fixture is unreachable. Risk mode confirm routes risky actions to a human." },
    { label: "Stops before confirm", cap: "artifacts/open_sub_account_review/v1.0.0.json → policy.allowlist", src: D.artifact_2.path,
      text: jsonHtml({ status: a2.status, success: a2.success, "policy.allowlist": a2.policy.allowlist, "policy.risk.mode": a2.policy.risk.mode }),
      note: "The capability ends on the review screen. Its least-privilege allowlist has no /confirm path, and the app's 'Confirm & Open Account' button matches the risky control patterns (confirm, open account), so the gate would send it to a human anyway." },
  ], (it) => {
    $("#safety-cap").textContent = it.cap;
    $("#safety-src").innerHTML = `<a href="${GH + it.src}">${esc(it.src)}</a>`;
    $("#safety-code").innerHTML = it.text;
    $("#safety-code").scrollTop = 0;
    $("#safety-note").textContent = it.note;
  });

  /* ---------------- 06 handoff ---------------- */
  bind("esc-path", E.path);
  bindHref("esc-path", GH_TREE + E.path);
  const escShot = E.screenshots[0];
  $("#esc-img").src = escShot;
  zoomable("#esc-shot");
  $("#esc-intervention").innerHTML = jsonHtml(E.intervention);
  $("#esc-resume").innerHTML = jsonHtml(E.resume);
  const ledger = $("#esc-ledger");
  const evs = E.result.control_events;
  evs.forEach((ev, i) => {
    const pillCls = ev.holder === "human" ? "human" : "neutral";
    ledger.append(el("li", { class: ev.holder }, [
      el("div", { class: "ev", html: `<span class="pill ${pillCls}">${esc(ev.holder)}</span> <b>${esc(ev.event)}</b> <span class="ts">${esc(fmtTime(ev.at))}</span>` }),
      el("div", { class: "det", text: ev.detail }),
    ]));
    const resumed = evs.find((x) => x.event === "resumed");
    if (ev.event === "ceded" && resumed) {
      const held = Math.round((ms(resumed.at) - ms(ev.at)) / 1000);
      ledger.append(el("div", { class: "gap", text: `human held the session for ${Math.floor(held / 60)}m ${held % 60}s (ceded → resumed); the operator's actions are collected by the page recorder and logged at hand-back` }));
    }
  });
  const skipped = E.result.steps.filter((s) => s.status === "skipped").map((s) => s.step_id);
  $("#esc-final").innerHTML = `Final status: <span class="pill ok">${esc(E.result.status)}</span> · outputs <code>${esc(JSON.stringify(E.result.outputs))}</code> · step ${esc(skipped.join(", "))} marked <code>skipped by ${esc(E.resume.operator)}</code>, then s5–s8 replayed deterministically.`;
  const ES = D.escalation_scripted;
  $("#esc-scripted").innerHTML = `A second run, <a href="${GH_TREE + ES.path}">${esc(ES.path)}</a>, drives the same protocol with a scripted operator (<code>${esc(ES.resume.operator)}</code>, resolution <code>${esc(ES.resume.resolution)}</code>), recorded with <code>demo/escalation_demo.py --simulate-operator</code>.`;

  /* ---------------- 07 evidence ---------------- */
  const tree = [
    ["evidence/discovery/<run>/", "tree/main/" + disc.path, [
      ["run.jsonl", "structured events"], ["transcript.jsonl", "per-step model request/response, usage, response ids"],
      ["trace.json", "typed record: observation, intent, decision, element, result"], ["screenshots/step-NN.png", "one per step"]]],
    ["evidence/replay-success/<run>/", "tree/main/" + D.replays.recovery.path, [["result.json", "the result contract, steps, recoveries, drift"], ["run.jsonl", "events"]]],
    ["evidence/replay-error/<run>/", "tree/main/" + D.replays.failure.path, [["result.json", "outcome or error with expected/observed"], ["run.jsonl", ""], ["screenshots/outcome-*.png | failure-*.png", "on failure only"]]],
    ["evidence/escalation/<run>/", "tree/main/" + E.path, [["intervention.json", "the request"], ["resume.json", "the operator's answer"], ["result.json", "includes control_events"], ["screenshots/escalation-*.png", ""]]],
  ];
  const tr = $("#ev-tree");
  tree.forEach(([dir, href, files]) => {
    tr.append(el("div", {}, el("a", { class: "dir", href: "https://github.com/ishan0410/reprise/" + href, text: dir })));
    const ind = el("div", { class: "indent" });
    files.forEach(([f, note]) => ind.append(el("div", { html: `<span class="file">${esc(f)}</span> ${note ? `<span class="note">· ${esc(note)}</span>` : ""}` })));
    tr.append(ind);
  });
  tr.append(el("p", { class: "small muted", style: "font-family:var(--sans);margin:12px 0 0", html: `Indexed in <a href="${GH}evidence/README.md">evidence/README.md</a>. No Groq fallback fired in either recorded discovery run (<code>provider_events: []</code>).` }));

  const jsonl = (rows) => rows.map((r) => esc(JSON.stringify(r))).join("\n");
  tabs("#log-tabs", [
    { label: "escalation run.jsonl", run: E },
    { label: "recovery run.jsonl", run: D.replays.recovery },
    { label: "discovery run.jsonl", run: disc },
  ], (it) => {
    $("#log-cap").textContent = `${it.run.path}/run.jsonl`;
    $("#log-src").innerHTML = `<a href="${GH + it.run.path}/run.jsonl">view on GitHub</a>`;
    $("#log-code").innerHTML = highlightLines(jsonl(it.run.run_log), (l) => /escalation\.|recover|RestartPhase|discovery\.(start|end)|REDACTED/.test(l));
    $("#log-code").scrollTop = 0;
  });

  /* ---------------- nav highlighting ---------------- */
  const links = [...document.querySelectorAll(".nav-links a")];
  const byId = new Map(links.map((a) => [a.getAttribute("href").slice(1), a]));
  if ("IntersectionObserver" in window) {
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        if (e.isIntersecting) {
          links.forEach((a) => a.classList.remove("active"));
          const a = byId.get(e.target.id);
          if (a) a.classList.add("active");
        }
      });
    }, { rootMargin: "-45% 0px -50% 0px" });
    document.querySelectorAll("section.block").forEach((s) => io.observe(s));
  }
})();
