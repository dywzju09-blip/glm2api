/* GLM Studio 管理面板 */
(function () {
  "use strict";

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const store = {
    key: localStorage.getItem("glm_admin_key") || "",
  };

  // ---------- 工具 ----------
  function fmtNum(n) {
    n = Number(n || 0);
    if (n >= 1e8) return (n / 1e8).toFixed(2) + " 亿";
    if (n >= 1e4) return (n / 1e4).toFixed(2) + " 万";
    return n.toLocaleString("zh-CN");
  }
  function fmtTokens(n) {
    n = Number(n || 0);
    if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
    return String(n);
  }
  function fmtTime(ts) {
    if (!ts) return "-";
    const d = new Date(ts * 1000);
    const p = (x) => String(x).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }
  function ago(ts) {
    if (!ts) return "从未";
    const s = Math.floor(Date.now() / 1000 - ts);
    if (s < 60) return s + " 秒前";
    if (s < 3600) return Math.floor(s / 60) + " 分钟前";
    if (s < 86400) return Math.floor(s / 3600) + " 小时前";
    return Math.floor(s / 86400) + " 天前";
  }

  function toast(msg, type) {
    const el = document.createElement("div");
    el.className = "toast " + (type || "");
    el.textContent = msg;
    $("#toasts").appendChild(el);
    setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .3s"; }, 2600);
    setTimeout(() => el.remove(), 3000);
  }

  async function api(path, opts) {
    opts = opts || {};
    const res = await fetch("/admin/api" + path, {
      method: opts.method || "GET",
      headers: {
        "Content-Type": "application/json",
        "X-Admin-Key": store.key,
      },
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    if (res.status === 401) {
      logout();
      throw new Error("登录已失效");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || res.statusText || "请求失败");
    return data;
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        () => toast("已复制到剪贴板", "ok"), () => toast("复制失败", "err"));
    } else {
      const ta = document.createElement("textarea");
      ta.value = text; document.body.appendChild(ta);
      ta.select(); document.execCommand("copy"); ta.remove();
      toast("已复制到剪贴板", "ok");
    }
  }

  // ---------- 组件片段 ----------
  function barBlock(caption, used, limit) {
    let pct = 0, cls = "", right;
    if (limit > 0) {
      pct = Math.min(100, (used / limit) * 100);
      cls = pct >= 90 ? " full" : pct >= 70 ? " warn" : "";
      right = `${fmtTokens(used)} / ${fmtTokens(limit)}`;
    } else {
      right = fmtTokens(used);
    }
    return `<div class="bar-block">
      <div class="bar-caption"><span>${caption}</span><b>${right}</b></div>
      <div class="bar${cls}"><i style="width:${pct}%"></i></div>
    </div>`;
  }

  const TYPE_LABEL = { web: "网页账号", guest: "游客", official: "官方Key" };
  const TYPE_CLASS = { web: "web", guest: "guest", official: "official" };
  const STATE_LABEL = { active: "正常", cooldown: "冷却中", exhausted: "已限额", disabled: "已禁用" };

  function stateBadge(a) {
    if (a.state === "cooldown" && a.cooldown_left > 0) {
      return `<span class="badge state-cooldown">冷却 ${a.cooldown_left}s</span>`;
    }
    return `<span class="badge state-${a.state}">${STATE_LABEL[a.state] || a.state}</span>`;
  }

  // ---------- 页面：仪表盘 ----------
  async function viewDashboard() {
    const data = await api("/overview");
    const u = data.usage;
    const c = data.counts;
    const cards = `
      <div class="cards">
        ${metricCard("今日请求", fmtNum(u.today_requests), `Token ${fmtTokens(u.today_tokens)}`)}
        ${metricCard("5 小时窗口", fmtNum(u.hour5_requests), `Token ${fmtTokens(u.hour5_tokens)}`)}
        ${metricCard("7 天用量", fmtTokens(u.week_tokens), `请求 ${fmtNum(u.week_requests)}`)}
        ${metricCard("7 天成功率", data.success_rate_7d == null ? "—" : data.success_rate_7d + "%", "",
          data.success_rate_7d == null ? "" : (data.success_rate_7d >= 95 ? "ok" : "warn"))}
        ${metricCard("账号池", `${c.accounts_active}/${c.accounts_total}`, `网页 ${c.web_accounts} · 官方 ${c.official_accounts}`,
          c.accounts_active === 0 && c.accounts_total > 0 ? "warn" : "ok")}
        ${metricCard("对外 Keys", `${c.keys_active}/${c.keys_total}`, c.open_mode ? "⚠ 开放模式（无 Key 校验）" : "已开启鉴权")}
      </div>`;

    $("#view").innerHTML = `
      <div class="page-head"><h1>仪表盘</h1><span class="sub">整体状态与每日 Token 用量趋势</span></div>
      ${c.open_mode ? `<div class="notice">当前处于<b>开放模式</b>：还没有创建任何对外 Key，所有 /v1 接口无需鉴权即可调用。请尽快到「API Keys」创建 Key。</div>` : ""}
      ${cards}
      <div class="panel" style="margin-top:6px">
        <div class="panel-head">近 14 天 Token 用量<span class="sub">悬停柱子查看当日请求次数与 Token 明细</span></div>
        <div class="chart-wrap">${dailyChart(data.daily || [])}</div>
      </div>`;
  }

  function niceCeil(v) {
    if (v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    for (const m of [1, 2, 5, 10]) if (m * p >= v) return m * p;
    return v;
  }

  function dailyChart(daily) {
    const W = 880, H = 300, padL = 58, padR = 18, padT = 30, padB = 36;
    const innerW = W - padL - padR, innerH = H - padT - padB;
    const n = daily.length || 1;
    const maxTok = Math.max(...daily.map((d) => d.tokens), 0);
    const top = niceCeil(maxTok);
    const bw = Math.max(12, Math.min(46, (innerW / n) * 0.62));
    const parts = [];

    parts.push(`<defs>
      <linearGradient id="gBar" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="#8b5cf6"/><stop offset="100%" stop-color="#4f46e5"/>
      </linearGradient>
      <linearGradient id="gBarToday" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="#34d399"/><stop offset="100%" stop-color="#059669"/>
      </linearGradient>
    </defs>`);

    for (let i = 0; i <= 4; i++) {
      const v = (top * i) / 4;
      const y = padT + innerH - (innerH * i) / 4;
      parts.push(`<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}" class="grid"/>`);
      parts.push(`<text x="${padL - 9}" y="${y + 4}" text-anchor="end" class="axis">${fmtTokens(v)}</text>`);
    }
    parts.push(`<line x1="${padL}" y1="${padT + innerH}" x2="${W - padR}" y2="${padT + innerH}" stroke="var(--border)" stroke-width="1.2"/>`);

    daily.forEach((d, i) => {
      const cx = padL + (innerW * (i + 0.5)) / n;
      const h = top > 0 ? (d.tokens / top) * innerH : 0;
      const y = padT + innerH - h;
      const isToday = i === n - 1;
      if (d.tokens > 0) {
        parts.push(`<rect x="${(cx - bw / 2).toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" ` +
          `height="${Math.max(h, 2).toFixed(1)}" rx="5" fill="url(#${isToday ? "gBarToday" : "gBar"})" ` +
          `class="cbar${isToday ? " today" : ""}" opacity="0.92"` +
          `><title>${d.date}（${isToday ? "今天" : "周" + "日一二三四五六"[new Date(d.date + "T12:00:00").getDay()]}）\n请求：${fmtNum(d.requests)} 次\nToken：${fmtNum(d.tokens)}</title></rect>`);
        parts.push(`<text x="${cx.toFixed(1)}" y="${(y - 7).toFixed(1)}" text-anchor="middle" class="axis" style="font-size:10px">${fmtTokens(d.tokens)}</text>`);
      } else {
        parts.push(`<rect x="${(cx - 1.5).toFixed(1)}" y="${padT + innerH - 3}" width="3" height="3" rx="1.5" class="cbar-empty"/>`);
      }
      if (i % 2 === 1 || isToday) {
        parts.push(`<text x="${cx.toFixed(1)}" y="${H - 12}" text-anchor="middle" class="axis">${d.date.slice(5)}</text>`);
      }
    });
    if (maxTok === 0) {
      parts.push(`<text x="${W / 2}" y="${padT + innerH / 2}" text-anchor="middle" class="axis" style="font-size:13px">暂无用量数据 — 通过 /v1 接口发起请求后，这里会展示每日 Token 用量</text>`);
    }
    return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" class="usage-chart">${parts.join("")}</svg>`;
  }

  function metricCard(label, value, detail, valueClass) {
    return `<div class="card metric">
      <div class="label">${label}</div>
      <div class="value ${valueClass || ""}">${value}</div>
      ${detail ? `<div class="detail">${detail}</div>` : ""}
    </div>`;
  }

  // ---------- 页面：账号池 ----------
  function quotaBars(a) {
    const q = a.quota;
    if (!q || !q.windows || !q.windows.length) {
      return `<span class="muted" style="font-size:11px">点击「测试」拉取官方额度</span>`;
    }
    const level = q.level ? `<span class="badge official" style="margin-left:6px">${esc(q.level)} 档</span>` : "";
    const bars = q.windows.map((w) => {
      let pct = Math.min(100, w.percent || 0), cls = "";
      if (pct >= 90) cls = " full"; else if (pct >= 70) cls = " warn";
      const reset = w.reset_at ? `，${fmtTime(w.reset_at)} 重置` : "";
      return `<div class="cell-bars">
        <div class="bar-caption"><span>${esc(w.label)} 窗口</span>
          <b>${fmtNum(w.used)} / ${fmtNum(w.total)}（${w.percent}%）</b></div>
        <div class="bar${cls}" title="剩余 ${fmtNum(w.remaining)}${reset}"><i style="width:${pct.toFixed(1)}%"></i></div>
      </div>`;
    }).join("");
    const updated = q.updated_at ? `<div class="muted" style="font-size:10.5px;margin-top:3px">${ago(q.updated_at)}更新</div>` : "";
    return `<div style="display:flex;flex-direction:column;gap:4px">${bars}${updated}</div>${level}`;
  }

  function usageCell(a) {
    if (a.type === "official") return quotaBars(a);
    return `<div class="cell-bars">
      ${barBlock("5h · " + fmtNum(a.usage.req_5h) + " 次", a.usage.tokens_5h, a.limit_5h).replace("Token", "")}
      ${barBlock("7d · " + fmtNum(a.usage.req_7d) + " 次", a.usage.tokens_7d, a.limit_7d).replace("Token", "")}
    </div>`;
  }

  async function viewAccounts() {
    const data = await api("/accounts");
    const rows = data.accounts.map((a) => `
      <tr>
        <td><b>${esc(a.name)}</b><br><span class="muted" style="font-size:11px">${esc(a.note || a.secret_preview)}</span></td>
        <td><span class="badge ${TYPE_CLASS[a.type]}">${TYPE_LABEL[a.type]}</span></td>
        <td>${stateBadge(a)}</td>
        <td class="num">${a.priority}</td>
        <td>${usageCell(a)}</td>
        <td class="num">
          <div>${fmtNum(a.usage.req_today)}</div>
          <div class="muted" style="font-size:11px">${fmtTokens(a.usage.tokens_today)} tok</div>
        </td>
        <td class="num">${a.success_rate_7d == null ? "—" : a.success_rate_7d + "%"}</td>
        <td class="muted" style="font-size:11.5px">${ago(a.last_used_at)}</td>
        <td>
          <div class="actions">
            <button class="btn sm" data-act="test" data-id="${a.id}">测试</button>
            ${a.type === "official" ? `<button class="btn sm" data-act="quota" data-id="${a.id}">刷新额度</button>` : ""}
            <button class="btn sm" data-act="edit" data-id="${a.id}">编辑</button>
            ${a.status === "active"
              ? `<button class="btn sm" data-act="disable" data-id="${a.id}">禁用</button>`
              : `<button class="btn sm" data-act="enable" data-id="${a.id}">启用</button>`}
            <button class="btn sm danger-ghost" data-act="del" data-id="${a.id}">删除</button>
          </div>
        </td>
      </tr>`).join("");

    $("#view").innerHTML = `
      <div class="page-head">
        <h1>账号池</h1><span class="sub">添加 GLM 网页账号（refresh_token）或智谱官方 API Key，统一调度轮询</span>
        <div class="spacer"></div>
        <button class="btn primary" id="add-account-btn">＋ 添加账号</button>
      </div>
      <div class="panel table-wrap">
        ${data.accounts.length ? `
        <table class="wide">
          <thead><tr>
            <th>账号</th><th>类型</th><th>状态</th><th class="num">优先级</th>
            <th>套餐额度 / 用量</th><th class="num">今日</th><th class="num">7天成功率</th><th>最近使用</th><th style="text-align:right">操作</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>` : `<div class="empty"><div class="big">还没有账号</div>点击右上角「添加账号」开始<br>支持 chatglm.cn 网页账号 refresh_token、游客模式、智谱开放平台 API Key</div>`}
      </div>`;

    $("#add-account-btn").onclick = () => accountModal(null);
    $$("button[data-act]").forEach((btn) => {
      btn.onclick = async () => {
        const id = Number(btn.dataset.id);
        const act = btn.dataset.act;
        try {
          if (act === "test") {
            btn.textContent = "测试中"; btn.disabled = true;
            const r = await api(`/accounts/${id}/test`, { method: "POST" });
            toast(r.ok ? "✓ " + r.message : "✗ " + r.message, r.ok ? "ok" : "err");
            viewAccounts();
          } else if (act === "quota") {
            btn.textContent = "刷新中"; btn.disabled = true;
            const r = await api(`/accounts/${id}/quota`, { method: "POST" });
            toast("✓ 额度已刷新", "ok");
            viewAccounts();
          } else if (act === "edit") {
            const a = data.accounts.find((x) => x.id === id);
            accountModal(a);
          } else if (act === "disable" || act === "enable") {
            await api(`/accounts/${id}`, { method: "PATCH", body: { status: act === "enable" ? "active" : "disabled" } });
            toast(act === "enable" ? "已启用" : "已禁用", "ok");
            viewAccounts();
          } else if (act === "del") {
            if (!confirm("确定删除该账号？此操作不可恢复。")) return;
            await api(`/accounts/${id}`, { method: "DELETE" });
            toast("已删除", "ok");
            viewAccounts();
          }
        } catch (e) { toast(e.message, "err"); }
      };
    });
  }

  function accountModal(a) {
    const isEdit = !!a;
    const type = a ? a.type : "web";
    $("#modal-root").innerHTML = `
      <div class="modal-mask" id="m-mask">
        <div class="modal">
          <div class="modal-head">${isEdit ? "编辑账号" : "添加账号"}<button class="x" id="m-close">×</button></div>
          <div class="modal-body">
            ${isEdit ? "" : `
            <div class="field">
              <label>账号类型</label>
              <div class="radio-cards" id="type-cards">
                <label class="radio-card ${type === "web" ? "active" : ""}" data-type="web">
                  <input type="radio" name="type" value="web">
                  <div class="t">网页账号</div><div class="d">chatglm.cn refresh_token</div>
                </label>
                <label class="radio-card ${type === "guest" ? "active" : ""}" data-type="guest">
                  <input type="radio" name="type" value="guest">
                  <div class="t">游客</div><div class="d">免登录匿名通道</div>
                </label>
                <label class="radio-card ${type === "official" ? "active" : ""}" data-type="official">
                  <input type="radio" name="type" value="official">
                  <div class="t">官方 API Key</div><div class="d">智谱开放平台</div>
                </label>
              </div>
            </div>`}
            <div class="form-grid">
              <div class="field">
                <label>名称 *</label>
                <input type="text" id="f-name" value="${esc(a ? a.name : "")}" placeholder="例如：主力号-13800xxx">
              </div>
              <div class="field">
                <label>优先级（小者优先）</label>
                <input type="number" id="f-priority" value="${a ? a.priority : 0}">
              </div>
              <div class="field span2" id="f-secret-wrap">
                <label id="f-secret-label">refresh_token *</label>
                <textarea id="f-secret" placeholder="${type === "official" ? "例如：xxxxxxxx.xxxxxxxx（智谱开放平台的 API Key）" : "登录 chatglm.cn 后从 Cookie 中提取（__Secure-next-auth.session-token 或 refresh_token）"}">${""}</textarea>
                <div class="hint" id="f-secret-hint"></div>
              </div>
              <div class="field span2" id="f-baseurl-wrap" style="display:none">
                <label>API Base URL（CodingPlan 留空即可）</label>
                <input type="text" id="f-baseurl" value="${esc(a ? a.base_url : "")}" placeholder="留空 = CodingPlan 专属端点（推荐）">
                <div class="hint">CodingPlan 套餐 Key 直接留空；仅当添加普通按量付费 Key 时才填 https://open.bigmodel.cn/api/paas/v4</div>
              </div>
              <div class="field">
                <label>5h Token 限额（0 不限）</label>
                <input type="number" id="f-l5" value="${a ? a.limit_5h : 0}">
              </div>
              <div class="field">
                <label>7d Token 限额（0 不限）</label>
                <input type="number" id="f-l7" value="${a ? a.limit_7d : 0}">
              </div>
              <div class="field">
                <label>每日请求限额（0 不限）</label>
                <input type="number" id="f-ld" value="${a ? a.limit_daily : 0}">
              </div>
              <div class="field">
                <label>备注</label>
                <input type="text" id="f-note" value="${esc(a ? a.note : "")}" placeholder="可选">
              </div>
            </div>
          </div>
          <div class="modal-foot">
            <button class="btn" id="m-cancel">取消</button>
            <button class="btn primary" id="m-save">${isEdit ? "保存" : "添加"}</button>
          </div>
        </div>
      </div>`;

    let currentType = type;
    const secretHints = {
      web: "获取方法：浏览器登录 chatglm.cn → F12 → Application → Cookies → 复制 __Secure-next-auth.session-token 的值",
      guest: "游客模式无需填写任何凭据，额度最低，仅建议试跑用",
      official: "在 open.bigmodel.cn 控制台创建 API Key（CodingPlan 套餐直接粘贴，Base URL 留空即可）",
    };

    function refreshTypeUI() {
      $$("#type-cards .radio-card").forEach((c) =>
        c.classList.toggle("active", c.dataset.type === currentType));
      $("#f-secret-label").textContent = currentType === "official" ? "API Key *" : "refresh_token（游客可留空）";
      $("#f-secret-hint").textContent = secretHints[currentType];
      $("#f-secret").placeholder = currentType === "official"
        ? "智谱开放平台 API Key" : "留空则使用游客通道";
      $("#f-baseurl-wrap").style.display = currentType === "official" ? "" : "none";
    }

    $$("#type-cards .radio-card").forEach((c) => {
      c.onclick = () => { currentType = c.dataset.type; refreshTypeUI(); };
    });
    refreshTypeUI();

    const close = () => $("#m-mask").remove();
    $("#m-close").onclick = close;
    $("#m-cancel").onclick = close;
    $("#m-mask").addEventListener("click", (e) => { if (e.target.id === "m-mask") close(); });

    $("#m-save").onclick = async () => {
      const body = {
        name: $("#f-name").value.trim(),
        type: isEdit ? a.type : currentType,
        secret: $("#f-secret").value.trim(),
        priority: Number($("#f-priority").value || 0),
        limit_5h: Number($("#f-l5").value || 0),
        limit_7d: Number($("#f-l7").value || 0),
        limit_daily: Number($("#f-ld").value || 0),
        note: $("#f-note").value.trim(),
      };
      const b = $("#f-baseurl");
      if (b) body.base_url = b.value.trim();
      if (!body.name) return toast("请填写名称", "err");
      if (!isEdit && body.type === "web" && !body.secret) return toast("网页账号需要 refresh_token", "err");
      if (!isEdit && body.type === "official" && !body.secret) return toast("官方账号需要 API Key", "err");
      if (isEdit && !body.secret) delete body.secret;
      try {
        if (isEdit) await api(`/accounts/${a.id}`, { method: "PATCH", body });
        else await api("/accounts", { method: "POST", body });
        toast(isEdit ? "已保存" : "已添加", "ok");
        close();
        viewAccounts();
      } catch (e) { toast(e.message, "err"); }
    };
  }

  // ---------- 页面：API Keys ----------
  async function viewKeys() {
    const data = await api("/keys");
    const rows = data.keys.map((k) => `
      <tr>
        <td><b>${esc(k.name)}</b>${k.note ? `<br><span class="muted" style="font-size:11px">${esc(k.note)}</span>` : ""}</td>
        <td><code class="inline mono">${esc(k.key.slice(0, 12))}…${esc(k.key.slice(-4))}</code>
          <button class="btn sm" data-copy="${esc(k.key)}" style="margin-left:6px">复制</button></td>
        <td>${k.status === "active" ? '<span class="badge state-active">启用</span>' : '<span class="badge state-disabled">禁用</span>'}</td>
        <td class="num">
          <div>${fmtNum(k.usage_today.req_today)}${k.daily_limit ? " / " + fmtNum(k.daily_limit) : ""}</div>
          <div class="muted" style="font-size:11px">${fmtTokens(k.usage_today.tokens_today)} tok</div>
        </td>
        <td class="num">${k.rpm_limit || "不限"}</td>
        <td class="muted" style="font-size:11.5px">${k.allowed_models ? esc(k.allowed_models) : "全部模型"}</td>
        <td class="muted" style="font-size:11.5px">${ago(k.last_used_at)}</td>
        <td>
          <div class="actions">
            <button class="btn sm" data-act="edit" data-id="${k.id}">编辑</button>
            ${k.status === "active"
              ? `<button class="btn sm" data-act="disable" data-id="${k.id}">禁用</button>`
              : `<button class="btn sm" data-act="enable" data-id="${k.id}">启用</button>`}
            <button class="btn sm danger-ghost" data-act="del" data-id="${k.id}">删除</button>
          </div>
        </td>
      </tr>`).join("");

    $("#view").innerHTML = `
      <div class="page-head">
        <h1>API Keys</h1><span class="sub">对外发放的统一 Key：调用方用它访问 /v1 接口，可设置限流与模型白名单（额度请看账号池的套餐余量）</span>
        <div class="spacer"></div>
        <button class="btn primary" id="add-key-btn">＋ 创建 Key</button>
      </div>
      <div class="panel table-wrap">
        ${data.keys.length ? `
        <table class="wide">
          <thead><tr>
            <th>名称</th><th>Key</th><th>状态</th><th class="num">今日用量</th><th class="num">RPM</th>
            <th>模型白名单</th><th>最近使用</th><th style="text-align:right">操作</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>` : `<div class="empty"><div class="big">还没有创建 Key</div>当前 /v1 接口处于开放模式（无鉴权）<br>创建至少一个 Key 后，调用方必须携带 Authorization: Bearer &lt;key&gt;</div>`}
      </div>`;

    $("#add-key-btn").onclick = () => keyModal(null);
    $$("button[data-copy]").forEach((b) => b.onclick = () => copyText(b.dataset.copy));
    $$("button[data-act]").forEach((btn) => {
      btn.onclick = async () => {
        const id = Number(btn.dataset.id), act = btn.dataset.act;
        try {
          if (act === "edit") return keyModal(data.keys.find((x) => x.id === id));
          if (act === "disable" || act === "enable") {
            await api(`/keys/${id}`, { method: "PATCH", body: { status: act === "enable" ? "active" : "disabled" } });
            toast(act === "enable" ? "已启用" : "已禁用", "ok"); viewKeys();
          } else if (act === "del") {
            if (!confirm("确定删除该 Key？使用它的调用方将立即失效。")) return;
            await api(`/keys/${id}`, { method: "DELETE" }); toast("已删除", "ok"); viewKeys();
          }
        } catch (e) { toast(e.message, "err"); }
      };
    });
  }

  function keyModal(k) {
    const isEdit = !!k;
    $("#modal-root").innerHTML = `
      <div class="modal-mask" id="m-mask">
        <div class="modal" style="max-width:480px">
          <div class="modal-head">${isEdit ? "编辑 Key" : "创建 Key"}<button class="x" id="m-close">×</button></div>
          <div class="modal-body">
            <div class="field"><label>名称 *</label>
              <input type="text" id="f-name" value="${esc(k ? k.name : "")}" placeholder="例如：我的 ChatBox / 同事小王"></div>
            <div class="form-grid">
              <div class="field"><label>每日请求限额（0 不限）</label>
                <input type="number" id="f-daily" value="${k ? k.daily_limit : 0}"></div>
              <div class="field"><label>RPM 限额（0 不限）</label>
                <input type="number" id="f-rpm" value="${k ? k.rpm_limit : 0}"></div>
            </div>
            <div class="field"><label>模型白名单（逗号分隔，留空 = 全部）</label>
              <input type="text" id="f-models" value="${esc(k ? k.allowed_models : "")}" placeholder="例如：glm-5.2,glm-4-flash"></div>
            <div class="field"><label>备注</label>
              <input type="text" id="f-note" value="${esc(k ? k.note : "")}"></div>
          </div>
          <div class="modal-foot">
            <button class="btn" id="m-cancel">取消</button>
            <button class="btn primary" id="m-save">${isEdit ? "保存" : "创建"}</button>
          </div>
        </div>
      </div>`;
    const close = () => $("#m-mask").remove();
    $("#m-close").onclick = close; $("#m-cancel").onclick = close;
    $("#m-mask").addEventListener("click", (e) => { if (e.target.id === "m-mask") close(); });
    $("#m-save").onclick = async () => {
      const body = {
        name: $("#f-name").value.trim(),
        daily_limit: Number($("#f-daily").value || 0),
        rpm_limit: Number($("#f-rpm").value || 0),
        allowed_models: $("#f-models").value.trim(),
        note: $("#f-note").value.trim(),
      };
      if (!body.name) return toast("请填写名称", "err");
      try {
        if (isEdit) {
          await api(`/keys/${k.id}`, { method: "PATCH", body });
          toast("已保存", "ok"); close(); viewKeys();
        } else {
          const r = await api("/keys", { method: "POST", body });
          close();
          $("#modal-root").innerHTML = `
            <div class="modal-mask" id="m2-mask">
              <div class="modal" style="max-width:460px">
                <div class="modal-head">Key 创建成功<button class="x" id="m2-close">×</button></div>
                <div class="modal-body">
                  <div class="key-reveal"><span class="mono">${esc(r.key.key)}</span>
                    <button class="btn sm" id="m2-copy">复制</button></div>
                  <div class="hint" style="color:var(--text-3);font-size:12px;line-height:1.7">
                    请立即保存，Key 完整明文只在管理面板可见。<br>
                    调用方式：<code class="inline">Authorization: Bearer ${esc(r.key.key.slice(0, 16))}…</code><br>
                    端点：<code class="inline">POST /v1/chat/completions</code>
                  </div>
                </div>
                <div class="modal-foot"><button class="btn primary" id="m2-done">完成</button></div>
              </div>
            </div>`;
          const close2 = () => { $("#m2-mask").remove(); viewKeys(); };
          $("#m2-copy").onclick = () => copyText(r.key.key);
          $("#m2-close").onclick = close2;
          $("#m2-done").onclick = close2;
          $("#m2-mask").addEventListener("click", (e) => { if (e.target.id === "m2-mask") close2(); });
        }
      } catch (e) { toast(e.message, "err"); }
    };
  }

  // ---------- 页面：日志 ----------
  let logFilter = "";
  let logOffset = 0;

  async function viewLogs(append) {
    if (!append) logOffset = 0;
    const data = await api(`/logs?limit=50&offset=${logOffset}${logFilter ? "&status=" + logFilter : ""}`);
    const rows = data.events.map((e) => `
      <tr>
        <td class="muted" style="font-size:11.5px;white-space:nowrap">${fmtTime(e.ts)}</td>
        <td>${esc(e.key_name || "-")}</td>
        <td><code class="inline">${esc(e.model)}</code></td>
        <td><span class="badge ${e.channel === "official" ? "official" : "web"}">${e.channel === "official" ? "官方" : "网页"}</span> ${esc(e.account_name || "-")}</td>
        <td class="num">${fmtTokens(e.total_tokens)}</td>
        <td class="num">${(e.latency_ms / 1000).toFixed(1)}s</td>
        <td>${e.status === "success" ? '<span class="badge ok">成功</span>' : '<span class="badge fail">失败</span>'}</td>
        <td class="muted break" style="font-size:11px;max-width:260px">${esc(e.error || "")}</td>
      </tr>`).join("");

    if (append) {
      $("#log-tbody").insertAdjacentHTML("beforeend", rows);
    } else {
      $("#view").innerHTML = `
        <div class="page-head">
          <h1>请求日志</h1><span class="sub">最近的使用记录（保留 8 天）</span>
          <div class="spacer"></div>
          <div class="filters">
            <div class="seg" id="log-seg">
              <button data-f="" class="${!logFilter ? "active" : ""}">全部</button>
              <button data-f="success" class="${logFilter === "success" ? "active" : ""}">成功</button>
              <button data-f="error" class="${logFilter === "error" ? "active" : ""}">失败</button>
            </div>
            <button class="btn" id="log-refresh">刷新</button>
          </div>
        </div>
        <div class="panel table-wrap">
          ${data.events.length ? `
          <table class="wide">
            <thead><tr><th>时间</th><th>Key</th><th>模型</th><th>通道 / 账号</th>
              <th class="num">Token</th><th class="num">耗时</th><th>状态</th><th>错误</th></tr></thead>
            <tbody id="log-tbody">${rows}</tbody>
          </table>` : `<div class="empty"><div class="big">暂无记录</div>通过 /v1 接口发起请求后这里会出现日志</div>`}
        </div>
        ${data.events.length >= 50 ? `<div style="text-align:center;margin-top:14px">
          <button class="btn" id="log-more">加载更多</button></div>` : ""}`;
      $$("#log-seg button").forEach((b) => b.onclick = () => {
        logFilter = b.dataset.f; viewLogs(false);
      });
      $("#log-refresh").onclick = () => viewLogs(false);
      const more = $("#log-more");
      if (more) more.onclick = () => { logOffset += 50; viewLogs(true); };
    }
    const moreBtn = $("#log-more");
    if (moreBtn && append) {
      logOffset += 50;
      if (data.events.length < 50) moreBtn.remove();
    }
  }

  // ---------- 页面：设置 ----------
  async function viewSettings() {
    const data = await api("/settings");
    const s = data.settings;
    $("#view").innerHTML = `
      <div class="page-head"><h1>系统设置</h1><span class="sub">调度策略与上游参数，保存后立即生效</span></div>
      <div class="panel" style="max-width:720px;padding:20px 22px">
        <div class="field">
          <label>调度策略</label>
          <select id="s-strategy">
            <option value="round_robin" ${s.strategy === "round_robin" ? "selected" : ""}>轮询（同优先级账号轮流使用）</option>
            <option value="priority" ${s.strategy === "priority" ? "selected" : ""}>优先级（优先用排前面的账号）</option>
          </select>
        </div>
        <div class="switch-row">
          <label class="switch"><input type="checkbox" id="s-prefer-official" ${s.prefer_official ? "checked" : ""}><span class="track"></span></label>
          <span class="lbl"><b>优先官方通道</b>模型两个通道都支持时，优先走官方 API Key</span>
        </div>
        <div class="switch-row">
          <label class="switch"><input type="checkbox" id="s-auto-disable" ${s.auto_disable ? "checked" : ""}><span class="track"></span></label>
          <span class="lbl"><b>自动禁用</b>账号连续认证失败达到阈值后自动禁用</span>
        </div>
        <div class="form-grid">
          <div class="field"><label>自动禁用阈值（次）</label>
            <input type="number" id="s-threshold" value="${s.auto_disable_threshold}"></div>
          <div class="field"><label>单账号并发</label>
            <input type="number" id="s-conc" value="${s.max_concurrency_per_account}"></div>
          <div class="field"><label>上游超时（秒）</label>
            <input type="number" id="s-timeout" value="${s.request_timeout}"></div>
          <div class="field"><label>忙碌重试次数</label>
            <input type="number" id="s-busy" value="${s.busy_max_retries}"></div>
        </div>
        <div class="field">
          <label>官方 API Base URL</label>
          <input type="text" id="s-baseurl" value="${esc(s.official_base_url)}">
        </div>
        </div>
        <div class="field">
          <label>官方通道模型列表（每行一个）</label>
          <textarea id="s-models" style="min-height:120px">${esc((s.official_models || []).join("\n"))}</textarea>
          <div class="hint">请求的模型名命中此列表且存在官方 Key 账号时，可路由到官方通道。网页通道支持的模型：${fmtNum(data.web_models.length)} 个（glm-5.2 / glm-4.7 等及 -think / -search 变体）</div>
        </div>
        <div style="display:flex;justify-content:flex-end">
          <button class="btn primary" id="s-save">保存设置</button>
        </div>
      </div>`;

    $("#s-save").onclick = async () => {
      const body = {
        strategy: $("#s-strategy").value,
        prefer_official: $("#s-prefer-official").checked,
        auto_disable: $("#s-auto-disable").checked,
        auto_disable_threshold: Number($("#s-threshold").value || 3),
        max_concurrency_per_account: Number($("#s-conc").value || 2),
        request_timeout: Number($("#s-timeout").value || 120),
        busy_max_retries: Number($("#s-busy").value || 8),
        official_base_url: $("#s-baseurl").value.trim(),
        official_models: $("#s-models").value.split("\n").map((x) => x.trim()).filter(Boolean),
      };
      try {
        await api("/settings", { method: "PUT", body });
        toast("设置已保存", "ok");
      } catch (e) { toast(e.message, "err"); }
    };
  }

  // ---------- 路由 ----------
  const routes = {
    dashboard: { label: "仪表盘", icon: "M3 12h4l3-8 4 16 3-8h4", render: viewDashboard, auto: 10 },
    accounts: { label: "账号池", icon: "M12 2a4 4 0 110 8 4 4 0 010-8zM4 22a8 8 0 0116 0", render: viewAccounts, auto: 10 },
    keys: { label: "API Keys", icon: "M14 2l6 6v12a2 2 0 01-2 2H6a2 2 0 01-2-2V4a2 2 0 012-2h8zM14 2v6h6M8 13h8M8 17h5", render: viewKeys, auto: 15 },
    logs: { label: "请求日志", icon: "M4 4h16v4H4zM4 12h16v8H4zM8 6h.01M8 16h.01", render: () => viewLogs(false), auto: 20 },
    settings: { label: "系统设置", icon: "M12 15a3 3 0 100-6 3 3 0 000 6zM19.4 15a1.7 1.7 0 00.3 1.9l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.9-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1-1.6 1.7 1.7 0 00-1.9.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.9 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1a1.7 1.7 0 001.6-1 1.7 1.7 0 00-.3-1.9l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.9.3h0a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.6 1.7 1.7 0 001.9-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.9v0a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z", render: viewSettings, auto: 0 },
  };

  let currentRoute = "dashboard";
  let autoTimer = null;

  function nav() {
    $("#nav").innerHTML = Object.entries(routes).map(([id, r]) => `
      <button class="nav-item ${id === currentRoute ? "active" : ""}" data-route="${id}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
          stroke-linecap="round" stroke-linejoin="round"><path d="${r.icon}"/></svg>
        ${r.label}
      </button>`).join("");
    $$(".nav-item").forEach((b) => b.onclick = () => go(b.dataset.route));
  }

  async function go(route) {
    currentRoute = route;
    location.hash = route;
    nav();
    clearInterval(autoTimer);
    $("#view").innerHTML = `<div class="loading-block"><span class="spin"></span></div>`;
    try {
      await routes[route].render();
      if (routes[route].auto) {
        autoTimer = setInterval(() => {
          if (currentRoute === route && document.visibilityState === "visible") {
            routes[route].render().catch(() => {});
          }
        }, routes[route].auto * 1000);
      }
    } catch (e) {
      $("#view").innerHTML = `<div class="empty"><div class="big">加载失败</div>${esc(e.message)}</div>`;
    }
  }

  function logout() {
    localStorage.removeItem("glm_admin_key");
    store.key = "";
    clearInterval(autoTimer);
    $("#app").classList.remove("ready");
    $("#login").style.display = "";
    $("#login-key").value = "";
  }

  async function boot() {
    if (store.key) {
      try {
        const r = await api("/login", { method: "POST", body: { key: store.key } });
        $("#version").textContent = "v" + r.version;
        enterApp();
        return;
      } catch (e) { /* fallthrough to login */ }
    }
    $("#login").style.display = "";
    $("#login-key").focus();
  }

  function enterApp() {
    $("#login").style.display = "none";
    $("#app").classList.add("ready");
    const route = location.hash.replace("#", "") || "dashboard";
    go(routes[route] ? route : "dashboard");
  }

  $("#login-btn").onclick = async () => {
    const key = $("#login-key").value.trim();
    if (!key) return;
    $("#login-err").textContent = "";
    try {
      const res = await fetch("/admin/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail || "登录失败");
      }
      const r = await res.json();
      store.key = key;
      localStorage.setItem("glm_admin_key", key);
      $("#version").textContent = "v" + r.version;
      enterApp();
    } catch (e) { $("#login-err").textContent = e.message; }
  };
  $("#login-key").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#login-btn").click(); });
  $("#logout-btn").onclick = logout;
  window.addEventListener("hashchange", () => {
    const route = location.hash.replace("#", "");
    if (route && route !== currentRoute && routes[route]) go(route);
  });

  boot();
})();
