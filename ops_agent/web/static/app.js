// Ops support agent: upload -> checks (approve) -> message -> workbook -> save; plus search.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const VERDICT = {
  incorrect: "ไม่ถูกต้อง · Incorrect", unresolved: "ยังยืนยันไม่ได้ · Unresolved",
  verified: "ถูกต้อง · Verified", info: "ข้อสังเกต · Note",
};
let state = { session: null, companies: [], config: {}, picked: [], data: null, user: null };

async function api(path, opts = {}) {
  if (state.user) opts = { ...opts, headers: { ...(opts.headers || {}), Authorization: `Bearer ${await state.user.getIdToken()}` } };
  const r = await fetch(path, opts);
  if (!r.ok) {
    let body = {};
    try { body = await r.json(); } catch (_) {}
    const err = new Error(typeof body.detail === "string" ? body.detail : `Error ${r.status}`);
    err.body = body; err.status = r.status;
    throw err;
  }
  return r;
}
// Upload and check in the background, then poll: reading passports can take over a minute.
const sleep = (ms) => new Promise((ok) => setTimeout(ok, ms));
// The server can be briefly busy (429) or restarting (502/503/504): wait and try again.
async function patient(path, opts, tries = 8) {
  for (let k = 0; ; k++) {
    try { return await api(path, opts); }
    catch (e) { if (k >= tries || ![429, 502, 503, 504].includes(e.status)) throw e; await sleep(2000 + k * 1500); }
  }
}
// Word files with big pictures (a preparation letter full of photos) can push an upload past the
// server's 32 MB request limit. Only the text of such a file is read, so its pictures are swapped
// for a 1-pixel image before upload. Word files of pasted passport scans (little text) are left whole.
const TINY_PNG = Uint8Array.from(atob("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="), (c) => c.charCodeAt(0));
const MAX_UPLOAD = 30 * 1024 * 1024;
async function shrinkDocx(file) {
  if (!/\.docx$/i.test(file.name) || file.size < 4 * 1024 * 1024) return file;
  try {
    if (!window.JSZip) await loadScript("vendor/jszip.min.js");
    const zip = await JSZip.loadAsync(file);
    const doc = zip.file("word/document.xml");
    const text = doc ? (await doc.async("string")).replace(/<[^>]+>/g, "") : "";
    if (text.trim().length < 300) return file; // pasted passport scans: the pictures are the content
    zip.folder("word/media").forEach((path) => zip.file("word/media/" + path, TINY_PNG));
    const blob = await zip.generateAsync({ type: "blob", compression: "DEFLATE" });
    return new File([blob], file.name, { type: file.type });
  } catch (e) { return file; }
}
async function slim(fd) {
  const out = new FormData();
  let total = 0;
  for (const [k, v] of fd.entries()) {
    const f = v instanceof File ? await shrinkDocx(v) : v;
    total += f.size || 0;
    out.append(k, f, f.name);
  }
  if (total > MAX_UPLOAD) throw new Error(`ไฟล์รวมกันใหญ่เกินไป (${(total / 1048576).toFixed(0)} MB, สูงสุด 30 MB) ลองแยกตรวจทีละชุด`);
  return out;
}
async function checkFiles(fd, statusEl) {
  fd = await slim(fd);
  let j = await (await patient("/api/groups/check?background=1", { method: "POST", body: fd })).json();
  const id = j.job, t0 = Date.now();
  while (j.status === "running") {
    await sleep(2500);
    if (statusEl) statusEl.textContent = `กำลังอ่านและตรวจเอกสาร… ${Math.round((Date.now() - t0) / 1000)} วินาที`;
    j = await (await patient(`/api/jobs/${id}`, {}, 20)).json();
  }
  if (j.status === "error") { const e = new Error(j.detail); e.status = j.code; throw e; }
  return j.result;
}
const postJSON = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

// ---------- tabs
document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("hidden", v.id !== b.dataset.view));
}));

// ---------- upload
const drop = $("drop");
["dragenter", "dragover"].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (ev) => addFiles(ev.dataTransfer.files));
$("files").addEventListener("change", (ev) => { addFiles(ev.target.files); ev.target.value = ""; });

function addFiles(list) {
  for (const f of list) if (!state.picked.some((p) => p.name === f.name && p.size === f.size)) state.picked.push(f);
  renderPicked();
}
function renderPicked() {
  $("picked").innerHTML = state.picked.map((f, i) =>
    `<li><span>${esc(f.name)} <small>${(f.size / 1024).toFixed(0)} KB</small></span><button data-i="${i}" class="rm">ลบ</button></li>`).join("");
  $("picked").querySelectorAll(".rm").forEach((b) => b.addEventListener("click", () => { state.picked.splice(+b.dataset.i, 1); renderPicked(); }));
  $("btn-check").disabled = !state.picked.length;
}

$("btn-check").addEventListener("click", async () => {
  const fd = new FormData();
  state.picked.forEach((f) => fd.append("files", f, f.name));
  $("check-status").textContent = "กำลังอ่านและตรวจเอกสาร…";
  $("btn-check").disabled = true;
  try {
    state.data = await checkFiles(fd, $("check-status"));
    state.session = state.data.session;
    $("check-status").textContent = "";
    showChecks();
    ["step-checks", "step-message", "step-workbook"].forEach((id) => $(id).classList.remove("hidden"));
    $("step-save").classList.toggle("hidden", !state.config.store);
    $("msg-area").classList.add("hidden");
    $("step-checks").scrollIntoView({ behavior: "smooth" });
  } catch (e) {
    $("check-status").innerHTML = `<span class="err">${esc(e.message)}</span>`;
  } finally {
    $("btn-check").disabled = !state.picked.length;
  }
});

// ---------- checks
function showChecks() {
  const d = state.data, g = d.group;
  const fmt = (iso) => iso ? new Date(iso + "T00:00").toLocaleDateString("th-TH", { day: "numeric", month: "short", year: "2-digit" }) : "?";
  $("group-summary").innerHTML = g ? [
    ["รหัสกรุ๊ป · Tour code", g.tour_code], ["โปรแกรม · Program", `${g.program || ""} ${g.program_code ? "(" + g.program_code + ")" : ""}`],
    ["เดินทาง · Dates", `${fmt(g.start)} – ${fmt(g.end)}`],
    ["ผู้เดินทาง · Pax", `${g.customers} ลูกค้า + ${g.pax - g.customers} TL`],
    ["ห้อง · Rooms", `${g.rooms ?? "?"} ${Object.entries(g.room_mix || {}).map(([k, v]) => v + k).join(" + ")}`],
    ["หัวหน้าทัวร์ · TL", g.tour_leader || "?"],
  ].map(([k, v]) => `<div><b>${k}</b>${esc(v)}</div>`).join("") : "";
  const typeOpts = (cur) => d.types.map((o) => `<option value="${o.value}" ${o.value === cur ? "selected" : ""}>${esc(o.label)}</option>`).join("") +
    `<option value="unknown" ${cur === "unknown" || !cur ? "selected" : ""}>ไม่ใช้ไฟล์นี้ · Ignore</option>`;
  $("docs").innerHTML =
    `<div class="table-wrap"><table><tr><th>ไฟล์ · File</th><th>อ่านเป็น · Read as <small>(เปลี่ยนได้)</small></th><th></th></tr>` +
    d.documents.map((x) => `<tr><td>${esc(x.name)}</td><td><select class="retype" data-name="${esc(x.name)}">${typeOpts(x.type)}</select>${x.forced ? ' <small>เลือกเอง</small>' : ""}</td><td>${x.read ? '<span class="chip ok">อ่านแล้ว</span>' : `<span class="chip bad">อ่านไม่ได้</span> <small>${esc(x.note || "")}</small>`}</td></tr>`).join("") +
    `</table></div>` + (d.missing.length ? `<div class="missing">ยังไม่มีเอกสาร · Missing: ${d.missing.map(esc).join(", ")} — ข้อมูลที่ต้องใช้เอกสารนี้จะขึ้นเป็น "ยังยืนยันไม่ได้"</div>` : "");
  $("docs").querySelectorAll(".retype").forEach((s) => s.addEventListener("change", () => retype(s.dataset.name, s.value)));
  $("land").innerHTML = landHTML(d.land, fmt);

  const order = ["incorrect", "unresolved", "info", "verified"];
  $("findings").innerHTML = order.map((v) => {
    const fs = d.findings.filter((f) => f.verdict === v);
    if (!fs.length) return "";
    return `<div class="group-h"><span class="chip ${v}">${VERDICT[v]}</span> ${fs.length}</div>` + fs.map(findingHTML).join("");
  }).join("");
  $("findings").querySelectorAll("[data-approve]").forEach((b) => b.addEventListener("click", () =>
    approve(+b.dataset.approve, b.dataset.value === undefined ? null : d.findings[+b.dataset.approve].compare[+b.dataset.value].value)));
  $("findings").querySelectorAll("[data-unapprove]").forEach((b) => b.addEventListener("click", () => unapprove(+b.dataset.unapprove)));
  const picked = d.findings.some((f) => f.fixable_in_namelist && f.approval && f.approval.value);
  $("fix-namelist").classList.toggle("hidden", !picked);

  const n = d.blocking.length;
  $("gate").className = "gate " + (n ? "bad" : "ok");
  $("gate").textContent = n ? `ต้องอนุมัติหรือแก้อีก ${n} รายการ ก่อนร่างข้อความ` : "พร้อมร่างข้อความ · Ready to draft";
  $("btn-draft").disabled = !!n || !g;
  if (!g) { $("gate").className = "gate bad"; $("gate").textContent = "ต้องมีรายชื่อลูกค้า (Name list) ก่อนร่างข้อความและใบเบิก"; }
  $("btn-workbook").disabled = !g;
}

function landHTML(l, fmt) {
  if (!l) return "";
  const kv = [
    ["ผู้ประสานงานจีน · Land operator", [l.operator, l.sender && `(ผู้ส่ง ${l.sender})`].filter(Boolean).join(" ")],
    ["รหัสกรุ๊ปฝั่งจีน · Their code", l.tour_code], ["เส้นทาง · Route", l.route],
    ["วันที่ · Dates", l.start ? `${fmt(l.start)} – ${fmt(l.end)}` : null],
    ["จำนวน · Pax", l.adults != null ? `${l.adults} ลูกค้า + ${l.escorts || 0} TL` : null],
    ["หัวหน้าทัวร์ · TL", l.tour_leader], ["ไกด์ · Guide", l.guides.join(", ") || "ไม่ระบุ (导游 ว่าง)"],
    ["ผู้ติดต่ออื่น · Other contact", l.contacts.join(", ")],
    ["ห้อง · Rooms", Object.entries(l.rooms || {}).map(([k, v]) => `${v} ${k}${{"标准间": " (TWN)", "大床房": " (DBL)"}[k] || ""}`).join(", ")],
    ["โรงแรม · Standard", l.hotel_standard], ["มื้ออาหาร · Meals", l.meal_plan], ["รถ · Coach", l.coach],
    ["ยืนยันเมื่อ · Confirmed", l.confirmed_on ? fmt(l.confirmed_on) : null],
  ].filter(([, v]) => v);
  const days = l.days.length ? `<div class="table-wrap"><table><tr><th>วันที่</th><th>เช้า</th><th>กลางวัน</th><th>เย็น</th><th>โรงแรม</th></tr>` +
    l.days.map((x) => `<tr><td>${fmt(x.date)}</td><td>${esc(x.breakfast || "–")}</td><td>${esc(x.lunch || "–")}</td><td>${esc(x.dinner || "–")}</td><td>${esc(x.hotel || "–")}</td></tr>`).join("") +
    "</table></div>" : "";
  const warn = l.layout === "general" ? `<div class="missing">รูปแบบใบยืนยันใหม่ อ่านด้วยกฎทั่วไป กรุณาตรวจโรงแรม ไกด์ วันที่ และจำนวนคนกับไฟล์จริง · New layout, read with general rules</div>` : "";
  return `<div class="group-h">ข้อมูลจากใบยืนยันจีน · Land confirmation <small>${esc(l.file)}</small></div>${warn}
    <div class="summary">${kv.map(([k, v]) => `<div><b>${k}</b>${esc(v)}</div>`).join("")}</div>
    ${l.flights.length ? `<div class="d"><b>ไฟลต์ · Flights:</b> ${l.flights.map(esc).join(" · ")}</div>` : ""}
    ${days}
    ${l.itinerary.length ? `<details><summary>โปรแกรม (ภาษาจีน) · Itinerary</summary><div class="d">${l.itinerary.map(esc).join("<br>")}</div></details>` : ""}`;
}

function findingHTML(f) {
  const ev = f.evidence.length ? `<details><summary>หลักฐาน · Evidence (${f.evidence.length})</summary><ul>` +
    f.evidence.map((e) => `<li><b>${esc(e.file)}</b> ${esc(e.location || "")}${e.raw ? ` — <code>${esc(e.raw)}</code>` : ""}</li>`).join("") + "</ul></details>" : "";
  const canPick = (f.verdict === "incorrect" || f.verdict === "unresolved") && !f.fixed_by_workbook && f.compare.length > 1;
  const distinct = new Set(f.compare.map((c) => c.value)).size;
  const cmp = f.compare.length ? `<div class="table-wrap"><table class="cmp"><tr><th>เอกสาร · Source</th><th>ค่า · Value</th><th>ตำแหน่ง · Where</th>${canPick ? "<th></th>" : ""}</tr>` +
    f.compare.map((c, k) => {
      const chosen = f.approval && f.approval.value === c.value;
      const differs = distinct > 1 && c.value !== (f.compare.find((x) => x.used) || f.compare[0]).value;
      return `<tr class="${chosen ? "chosen" : c.used && !(f.approval && f.approval.value) ? "used" : ""} ${differs ? "differs" : ""}">
        <td>${esc(c.source)}${c.used ? ' <small>(ระบบเลือก)</small>' : ""}</td><td><b>${esc(c.value)}</b></td><td><small>${esc(c.location)}</small></td>
        ${canPick ? `<td>${chosen ? '<span class="chip ok">ใช้ค่านี้แล้ว</span>' : `<button data-approve="${f.id}" data-value="${k}">ใช้ค่านี้ <small>Use this</small></button>`}</td>` : ""}</tr>`;
    }).join("") + "</table></div>" : "";
  let act = "";
  if (f.verdict === "incorrect" && f.fixed_by_workbook) act = `<span class="chip info">แก้ให้อัตโนมัติในใบเบิก · Fixed in the workbook</span>`;
  else if (f.approval) act = `<span class="chip ok">${f.approval.value ? "เลือก: " + esc(f.approval.value) + " · " : "อนุมัติ · "}${esc(f.approval.by)} ${esc(f.approval.at.replace("T", " "))}</span> <button data-unapprove="${f.id}">ยกเลิก</button>`;
  else if (f.verdict === "incorrect") act = `<button class="primary" data-approve="${f.id}">ตรวจแล้ว ใช้ค่าที่ระบบเลือก <small>Approve</small></button>` +
      (f.accepted_value ? ` <span>ค่าที่จะใช้: <b>${esc(f.accepted_value)}</b></span>` : "");
  else if (f.accepted_value) act = `<span>ค่าที่ใช้: <b>${esc(f.accepted_value)}</b></span>`;
  return `<div class="finding ${f.verdict}"><div class="t">${esc(f.title)}</div>${f.detail ? `<div class="d">${esc(f.detail)}</div>` : ""}${cmp}${ev}${act ? `<div class="act">${act}</div>` : ""}</div>`;
}

async function approve(finding, value) {
  const by = $("approver").value.trim();
  if (!by) { $("approver").focus(); $("approver").scrollIntoView({ block: "center" }); $("gate").className = "gate bad"; $("gate").textContent = "กรอกชื่อผู้อนุมัติก่อน · Type your name first"; return; }
  try { state.data = await (await postJSON(`/api/sessions/${state.session}/approve`, { finding, by, value })).json(); showChecks(); }
  catch (e) { $("gate").textContent = e.message; }
}
async function unapprove(finding) {
  state.data = await (await api(`/api/sessions/${state.session}/approve/${finding}`, { method: "DELETE" })).json();
  showChecks();
}
async function retype(name, type) {
  $("gate").textContent = "กำลังอ่านใหม่… · Re-reading";
  try { state.data = await (await postJSON(`/api/sessions/${state.session}/retype`, { name, type })).json(); showChecks(); }
  catch (e) { $("gate").textContent = e.message; }
}
async function download(path, opts, statusEl, fallback) {
  const r = await api(path, opts);
  const blob = await r.blob();
  const name = /filename\*?=(?:UTF-8'')?"?([^";]+)/i.exec(r.headers.get("Content-Disposition") || "")?.[1] || fallback;
  const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: decodeURIComponent(name) });
  document.body.appendChild(a); a.click(); a.remove();
}
$("fix-namelist").addEventListener("click", async () => {
  try { await download(`/api/sessions/${state.session}/namelist`, {}, null, "NAMELIST-corrected.xlsx"); $("fix-status").textContent = "ดาวน์โหลดแล้ว ช่องที่แก้เป็นสีเหลือง มีหมายเหตุบอกค่าเดิม"; }
  catch (e) { $("fix-status").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
});

// ---------- message
$("btn-draft").addEventListener("click", async () => {
  $("draft-status").textContent = "กำลังร่าง…";
  const num = (id) => $(id).value === "" ? null : Number($(id).value);
  try {
    const body = {
      company_id: $("msg-company").value, today: $("msg-today").value || null,
      tip_per_pax_thb: num("msg-tip"), gift_budget_per_pax_thb: num("msg-gift"),
      sender_line_name: $("msg-line").value.trim() || "[ชื่อ LINE ของเรา]", sender_particle: $("msg-particle").value,
    };
    const r = await (await postJSON(`/api/sessions/${state.session}/message`, body)).json();
    $("msg-text").value = r.text;
    $("msg-area").classList.remove("hidden");
    showClaims(r.claims);
    $("draft-status").textContent = "";
  } catch (e) {
    $("draft-status").innerHTML = `<span class="err">${esc(e.message)}${e.body?.blocking ? ": " + e.body.blocking.map(esc).join("; ") : ""}</span>`;
  }
});

$("btn-review").addEventListener("click", async () => {
  const r = await (await postJSON(`/api/sessions/${state.session}/review`, {
    text: $("msg-text").value, company_id: $("msg-company").value,
    gift_budget_per_pax_thb: $("msg-gift").value === "" ? null : Number($("msg-gift").value),
  })).json();
  showClaims(r.claims);
});

function showClaims(claims) {
  const bad = claims.filter((c) => c.verdict === "incorrect").length;
  const un = claims.filter((c) => c.verdict === "unresolved").length;
  const sorted = [...claims].sort((a, b) => ["incorrect", "unresolved", "verified"].indexOf(a.verdict) - ["incorrect", "unresolved", "verified"].indexOf(b.verdict));
  $("claims").innerHTML = `<div class="group-h">ตรวจข้อความเทียบเอกสาร · Message check:
      <span class="chip ${bad ? "incorrect" : "verified"}">${bad} ไม่ถูกต้อง</span> <span class="chip unresolved">${un} ยืนยันไม่ได้</span>
      <span class="chip verified">${claims.length - bad - un} ถูกต้อง</span></div>
    <div class="table-wrap"><table><tr><th>ข้อความ · Claim</th><th>ผล</th><th>แหล่งที่มา · Source</th><th>หมายเหตุ</th></tr>` +
    sorted.map((c) => `<tr><td>${esc(c.claim)}</td><td><span class="chip ${c.verdict}">${esc(c.verdict)}</span></td><td>${esc(c.source)}</td><td>${esc(c.correction)}</td></tr>`).join("") +
    "</table></div>";
}

$("btn-copy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("msg-text").value); }
  catch (_) { $("msg-text").select(); document.execCommand("copy"); }
  $("copy-status").textContent = "คัดลอกแล้ว วางใน LINE ได้เลย · Copied";
  setTimeout(() => ($("copy-status").textContent = ""), 3000);
});

// ---------- workbook
$("btn-workbook").addEventListener("click", async () => {
  const selection = {};
  document.querySelectorAll("[data-role]").forEach((s) => (selection[s.dataset.role] = s.value));
  $("wb-status").textContent = "กำลังสร้างไฟล์…";
  try {
    const r = await postJSON(`/api/sessions/${state.session}/workbook`, {
      selection, gift_budget_per_pax_thb: $("msg-gift").value === "" ? null : Number($("msg-gift").value),
    });
    const blob = await r.blob();
    const name = /filename\*?=(?:UTF-8'')?"?([^";]+)/i.exec(r.headers.get("Content-Disposition") || "")?.[1] || "TL-expense.xlsx";
    const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: decodeURIComponent(name) });
    document.body.appendChild(a); a.click(); a.remove();
    $("wb-status").textContent = "ดาวน์โหลดแล้ว · Downloaded";
  } catch (e) { $("wb-status").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
});

// ---------- save
$("btn-save").addEventListener("click", async () => {
  $("save-status").textContent = "กำลังบันทึก…";
  try {
    const r = await (await postJSON(`/api/sessions/${state.session}/save`, { company_id: $("msg-company").value })).json();
    $("save-status").textContent = `บันทึกกรุ๊ป ${r.tour_code} แล้ว · Saved`;
  } catch (e) { $("save-status").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
});

// ---------- search
$("q").addEventListener("keydown", (e) => { if (e.key === "Enter") $("btn-search").click(); });
$("btn-search").addEventListener("click", async () => {
  const p = new URLSearchParams({ q: $("q").value, kind: $("q-kind").value, group: $("q-group").value.trim() });
  $("search-status").textContent = "กำลังค้นหา…";
  try {
    const hits = await (await api(`/api/search?${p}`)).json();
    $("search-status").textContent = `${hits.length} ผลลัพธ์`;
    $("hits").innerHTML = hits.map((h) => `<div class="hit"><div class="m">${esc(h.tour_code || "")} · ${esc(h.kind)} · ${esc(h.file_name || "")} ${esc(h.location || "")} · ${h.score.toFixed(2)}</div>${esc(h.content)}</div>`).join("");
  } catch (e) { $("search-status").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
});

// ---------- internal check (passports x name list x preparation letter)
const ipicked = [];
const idrop = $("i-drop");
["dragenter", "dragover"].forEach((e) => idrop.addEventListener(e, (ev) => { ev.preventDefault(); idrop.classList.add("over"); }));
["dragleave", "drop"].forEach((e) => idrop.addEventListener(e, (ev) => { ev.preventDefault(); idrop.classList.remove("over"); }));
idrop.addEventListener("drop", (ev) => iAdd(ev.dataTransfer.files));
$("i-files").addEventListener("change", (ev) => { iAdd(ev.target.files); ev.target.value = ""; });
function iAdd(list) {
  for (const f of list) if (!ipicked.some((p) => p.name === f.name && p.size === f.size)) ipicked.push(f);
  $("i-picked").innerHTML = ipicked.map((f, i) => `<li><span>${esc(f.name)} <small>${(f.size / 1024).toFixed(0)} KB</small></span><button data-i="${i}" class="rm">ลบ</button></li>`).join("");
  $("i-picked").querySelectorAll(".rm").forEach((b) => b.addEventListener("click", () => { ipicked.splice(+b.dataset.i, 1); iAdd([]); }));
  $("i-check").disabled = !ipicked.length;
}
const FIELD = { name: "ชื่อ · Name", passport_no: "เลขพาสปอร์ต", dob: "วันเกิด · DOB", expiry: "หมดอายุ · Expiry", sex: "เพศ · Sex", ticket: "ตั๋ว · Ticket" };
const EXP = { ok: ["ok", "ผ่าน"], short: ["bad", "ไม่ถึง 6 เดือน"], expired: ["bad", "หมดอายุ"], unknown: ["info", "ไม่ทราบ"] };
$("i-check").addEventListener("click", async () => {
  const fd = new FormData();
  ipicked.forEach((f) => fd.append("files", f, f.name));
  $("i-status").textContent = "กำลังอ่านไฟล์ (พาสปอร์ตใช้เวลาสักครู่)…";
  $("i-check").disabled = true;
  try {
    const s = await checkFiles(fd, $("i-status"));
    await showInternal(s);
    $("i-status").textContent = "";
  } catch (e) { $("i-status").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
  finally { $("i-check").disabled = !ipicked.length; }
});
async function showInternal(s) {
  const d = await (await api(`/api/sessions/${s.session}/internal`)).json();
  const bad = d.rows.filter((r) => r.problems).length;
  $("i-summary").innerHTML = [
    ["ผู้เดินทาง · Travellers", d.rows.length], ["พาสปอร์ตที่อ่านได้ · Passports read", d.has.passports],
    ["ตั๋วที่อ่านได้ · Tickets read", d.has.tickets], ["มีปัญหา · With problems", bad], ["วันกลับ · Return", d.trip_end || "ไม่ทราบ"],
  ].map(([k, v]) => `<div><b>${k}</b>${esc(v)}</div>`).join("");
  const miss = [!d.has.name_list && "รายชื่อลูกค้า", !d.has.prep_letter && "ใบนัดหมาย", !d.has.passports && "พาสปอร์ต", !d.has.tickets && "ตั๋ว / PNR"].filter(Boolean);
  $("i-docs").innerHTML = `<div class="table-wrap"><table><tr><th>ไฟล์</th><th>อ่านเป็น</th><th></th></tr>` +
    s.documents.map((x) => `<tr><td>${esc(x.name)}</td><td>${esc(x.label)}</td><td>${x.read ? '<span class="chip ok">อ่านแล้ว</span>' : `<span class="chip bad">อ่านไม่ได้</span> <small>${esc(x.note || "")}</small>`}</td></tr>`).join("") +
    "</table></div>" + (miss.length ? `<div class="missing">ยังไม่มี: ${miss.join(", ")}</div>` : "");
  const cell = (c) => c.field === "ticket" && c.status === "none" ? `<td class="muted">–</td>`
    : c.field === "ticket" && c.status === "missing" ? `<td class="differs"><b>ไม่พบตั๋ว</b></td>`
    : c.field === "ticket" && c.status === "differ" ? `<td class="differs"><b>${esc(c.passport)}</b><br><small>รายชื่อ: ${esc(c.name_list)}</small></td>`
    : c.status === "ok" ? `<td class="ok">${esc(c.passport)}</td>`
    : c.status === "differ" ? `<td class="differs"><b>${esc(c.passport)}</b><br><small>รายชื่อ: ${esc(c.name_list)}</small></td>`
    : c.status === "unread" ? `<td class="warn">อ่านไม่ชัด<br><small>รายชื่อ: ${esc(c.name_list || "–")}</small></td>`
    : `<td class="muted">${esc(c.name_list || "–")}${c.passport ? "" : "<br><small>ไม่มีในพาสปอร์ต</small>"}</td>`;
  $("i-table").innerHTML = d.rows.length ? `<div class="table-wrap"><table class="itable"><tr><th>#</th><th>ลูกค้า</th>${Object.values(FIELD).map((h) => `<th>${h}</th>`).join("")}<th>อายุพาสปอร์ต</th></tr>` +
    d.rows.map((r) => `<tr class="${r.problems ? "has-problem" : ""}"><td>${r.no ?? ""}</td><td>${esc(r.name_th || "")}<br><small>${r.passport_file ? esc(r.passport_file) : '<span class="err">ไม่พบพาสปอร์ต</span>'}</small></td>` +
      r.cells.map(cell).join("") + `<td><span class="chip ${EXP[r.expiry_status][0]}">${EXP[r.expiry_status][1]}</span><br><small>${esc(r.expiry_note)}</small></td></tr>`).join("") +
    "</table></div>" : `<div class="missing">ต้องมีรายชื่อลูกค้าเพื่อเทียบทีละคน</div>`;
  $("i-extra").innerHTML = d.extra_scans.length ? `<div class="missing">พาสปอร์ตที่ไม่มีในรายชื่อ: ${d.extra_scans.map((x) => esc(`${x.name} ${x.passport_no || ""} (${x.file})`)).join(", ")}</div>` : "";
  if (d.extra_tickets.length) $("i-extra").innerHTML += `<div class="missing">ตั๋วที่ไม่มีในรายชื่อ: ${d.extra_tickets.map((x) => esc(`${x.name} ${x.ticket_no || ""} (${x.file})`)).join(", ")}</div>`;
  const fs = d.finding_ids.map((i) => s.findings[i]).filter((f) => f.verdict !== "verified");
  $("i-findings").innerHTML = fs.length ? `<div class="group-h">รายชื่อ × ใบนัดหมาย × ตั๋ว · Name list vs preparation letter and tickets</div>` + fs.map(findingHTML).join("") : "";
  $("i-result").classList.remove("hidden");
  $("i-result").scrollIntoView({ behavior: "smooth" });
}

// ---------- sign-in (only when the server is set up for it)
const FB = "https://www.gstatic.com/firebasejs/10.14.1/";
const loadScript = (src) => new Promise((ok, fail) => document.head.appendChild(Object.assign(document.createElement("script"), { src, onload: ok, onerror: fail })));
function showSignIn(msg) {
  $("signin").classList.remove("hidden");
  $("signin-msg").textContent = msg || "";
}
async function signIn(cfg) {
  await loadScript(FB + "firebase-app-compat.js");
  await loadScript(FB + "firebase-auth-compat.js");
  firebase.initializeApp(cfg);
  const fauth = firebase.auth();
  $("btn-signin").onclick = () => fauth.signInWithPopup(new firebase.auth.GoogleAuthProvider()).catch((e) => showSignIn(e.message));
  $("btn-signout").onclick = () => fauth.signOut().then(() => location.reload());
  state.user = await new Promise((ok) => {
    const stop = fauth.onAuthStateChanged((u) => { if (u) { stop(); ok(u); } else showSignIn(); });
  });
  $("signin").classList.add("hidden");
  $("who").textContent = state.user.email;
  $("btn-signout").classList.remove("hidden");
}

// ---------- start
(async function init() {
  $("msg-today").value = new Date().toISOString().slice(0, 10);
  try {
    state.config = await (await api("/api/config")).json();
    if (state.config.firebase) await signIn(state.config.firebase);
    state.companies = await (await api("/api/companies")).json();
  } catch (e) {
    if (e.status === 401 || e.status === 403) return showSignIn(e.message);
    $("check-status").innerHTML = `<span class="err">เชื่อมต่อเซิร์ฟเวอร์ไม่ได้: ${esc(e.message)}</span>`; return;
  }
  if (state.config.font_css) document.head.appendChild(Object.assign(document.createElement("link"), { rel: "stylesheet", href: state.config.font_css }));
  const opts = state.companies.map((c) => `<option value="${c.id}">${esc(c.label)}</option>`).join("");
  $("msg-company").innerHTML = opts;
  $("roles").innerHTML = Object.entries(state.config.roles).map(([role, label]) =>
    `<label>${esc(label)} <select data-role="${role}">${opts}</select></label>`).join("");
  if (!state.config.store) document.querySelector('[data-view="search"]').title = "ยังไม่ได้ตั้งค่าฐานข้อมูล (OPS_DB_DSN)";
})();
