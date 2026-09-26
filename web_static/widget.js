/*
 * AI 客服聊天窗口。在任何网站的 </body> 前加一行就能用：
 *
 *   <script src="https://你的域名/widget.js" defer></script>
 *
 * 可选设置（写在同一个 script 标签上）：
 *   data-title="店铺名"         窗口标题
 *   data-color="#e8543a"        主色
 *   data-welcome="你好～"        开场白
 *   data-open="true"            打开页面就展开窗口
 *   data-lang="zh-TW"           语言（不写就跟网页 / 浏览器）：简体、繁體、English
 */
(function () {
  "use strict";
  if (window.__aiChatWidget) return;
  window.__aiChatWidget = true;

  var script = document.currentScript || (function () {
    var all = document.getElementsByTagName("script");
    for (var i = all.length - 1; i >= 0; i--) if (/widget\.js/.test(all[i].src)) return all[i];
    return null;
  })();
  var base = script && script.src ? new URL(script.src, location.href).origin : location.origin;
  var cfg = (script && script.dataset) || {};

  var TEXTS = {
    hans: {
      title: "AI 客服",
      status: "在线 · 一般几秒内回复",
      welcome: "你好呀 👋 我是 AI 助理，想了解什么直接问我～价格、怎么下单、要多久都可以问。",
      placeholder: "输入消息…",
      send: "发送",
      error: "网络有点问题，请稍后再试 🙏",
      slow: "消息有点多啦，稍等一下再发哦～",
      owner: "本人",
      open: "打开聊天",
      close: "关闭",
      powered: "AI 客服由 vinc的ai铺子 搭建"
    },
    hant: {
      title: "AI 客服",
      status: "在線 · 一般幾秒內回覆",
      welcome: "你好 👋 我是 AI 助理，想了解什麼直接問我～價格、怎麼下單、要多久都可以問。",
      placeholder: "輸入訊息…",
      send: "傳送",
      error: "網路有點問題，請稍後再試 🙏",
      slow: "訊息有點多囉，稍等一下再傳～",
      owner: "本人",
      open: "開啟聊天",
      close: "關閉",
      powered: "AI 客服由 vinc的ai鋪子 搭建"
    },
    en: {
      title: "AI Assistant",
      status: "Online · usually replies in seconds",
      welcome: "Hi there 👋 I'm the AI assistant. Ask me anything: prices, how to order, how long it takes.",
      placeholder: "Type a message…",
      send: "Send",
      error: "Connection problem, please try again in a moment 🙏",
      slow: "That's a lot of messages, please wait a moment.",
      owner: "Owner",
      open: "Open chat",
      close: "Close",
      powered: "AI assistant built by vinc's AI shop"
    }
  };
  // "zh-TW", "zh-HK", "zh-Hant" -> Traditional; other Chinese -> Simplified; the rest -> English.
  function pickLang(tag) {
    tag = String(tag || "").toLowerCase();
    if (!/^zh/.test(tag)) return "en";
    return /hant|tw|hk|mo/.test(tag) ? "hant" : "hans";
  }
  var langTag = cfg.lang || document.documentElement.lang || navigator.language || "";
  var lang = pickLang(langTag);
  var T = TEXTS[lang];
  var color = /^#[0-9a-f]{3,8}$/i.test(cfg.color || "") ? cfg.color : "#e8543a";

  // One random id per browser, so the conversation survives a page reload.
  function visitorId() {
    var id = null;
    try { id = localStorage.getItem("aiChatVisitor"); } catch (e) {}
    if (!id || !/^[A-Za-z0-9-]{8,64}$/.test(id)) {
      id = (window.crypto && crypto.randomUUID) ? crypto.randomUUID()
        : "v" + Date.now().toString(36) + Math.random().toString(36).slice(2, 12);
      try { localStorage.setItem("aiChatVisitor", id); } catch (e) {}
    }
    return id;
  }
  var visitor = visitorId();

  // The owner's own pages (data-track) count one page view; ?me=<token> marks
  // this browser as the owner's, so their own visits stay out of the numbers.
  if (script && script.hasAttribute("data-track")) {
    var params = new URLSearchParams(location.search);
    var me = params.get("me") || "";
    if (me) {
      params.delete("me");
      var rest = params.toString();
      try { history.replaceState(null, "", location.pathname + (rest ? "?" + rest : "") + location.hash); } catch (e) {}
    }
    fetch(base + "/api/hit", {
      method: "POST", headers: { "Content-Type": "application/json" }, keepalive: true,
      body: JSON.stringify({ v: visitor, page: location.pathname, ref: document.referrer.slice(0, 300),
        from: params.get("from") || params.get("utm_source") || "", lang: navigator.language || "",
        host: location.hostname, me: me })
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (me) alert(d && d.owner ? "好了 ✅ 这台设备已标记为你自己，以后不算进访客统计。" : "这个标记链接不对，没有标记成功。");
    }).catch(function () {});
  }

  var host = document.createElement("div");
  host.style.cssText = "position:fixed;z-index:2147483000;right:0;bottom:0;";
  var root = host.attachShadow ? host.attachShadow({ mode: "open" }) : host;
  root.innerHTML =
    "<style>" +
    ":host{all:initial}" +
    "*{box-sizing:border-box;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei','Segoe UI',Roboto,sans-serif}" +
    ".btn{position:fixed;right:20px;bottom:20px;width:60px;height:60px;border-radius:50%;border:0;cursor:pointer;background:var(--c);color:#fff;box-shadow:0 6px 20px rgba(0,0,0,.25);display:flex;align-items:center;justify-content:center;transition:transform .15s}" +
    ".btn:hover{transform:scale(1.06)}" +
    ".btn svg{width:28px;height:28px}" +
    ".badge{position:absolute;top:2px;right:2px;min-width:18px;height:18px;border-radius:9px;background:#ff3b30;color:#fff;font-size:11px;line-height:18px;text-align:center;padding:0 5px;display:none}" +
    ".panel{position:fixed;right:20px;bottom:92px;width:370px;height:560px;max-height:calc(100vh - 112px);background:#fff;border-radius:16px;box-shadow:0 12px 40px rgba(0,0,0,.25);display:none;flex-direction:column;overflow:hidden}" +
    ".panel.open{display:flex}" +
    ".head{background:var(--c);color:#fff;padding:14px 16px;display:flex;align-items:center;gap:10px}" +
    ".avatar{width:36px;height:36px;border-radius:50%;background:rgba(255,255,255,.25);display:flex;align-items:center;justify-content:center;font-size:18px;flex:none}" +
    ".who{flex:1;min-width:0}" +
    ".name{font-weight:600;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}" +
    ".status{font-size:12px;opacity:.9}" +
    ".x{background:none;border:0;color:#fff;font-size:22px;cursor:pointer;padding:4px 8px;line-height:1}" +
    ".log{flex:1;overflow-y:auto;padding:14px;background:#f6f6f7;display:flex;flex-direction:column;gap:8px}" +
    ".m{max-width:82%;padding:9px 12px;border-radius:14px;font-size:14px;line-height:1.5;white-space:pre-wrap;word-wrap:break-word;overflow-wrap:anywhere}" +
    ".m.bot,.m.owner{align-self:flex-start;background:#fff;color:#222;border-bottom-left-radius:4px;box-shadow:0 1px 2px rgba(0,0,0,.06)}" +
    ".m.me{align-self:flex-end;background:var(--c);color:#fff;border-bottom-right-radius:4px}" +
    ".tag{display:block;font-size:11px;color:var(--c);font-weight:600;margin-bottom:2px}" +
    ".m.err{align-self:center;background:none;color:#999;font-size:12px;box-shadow:none}" +
    ".typing{align-self:flex-start;background:#fff;border-radius:14px;padding:12px 14px;display:none;gap:4px}" +
    ".typing.on{display:flex}" +
    ".typing i{width:6px;height:6px;border-radius:50%;background:#bbb;animation:b 1.2s infinite}" +
    ".typing i:nth-child(2){animation-delay:.2s}.typing i:nth-child(3){animation-delay:.4s}" +
    "@keyframes b{0%,60%,100%{opacity:.3;transform:translateY(0)}30%{opacity:1;transform:translateY(-3px)}}" +
    ".bar{display:flex;gap:8px;padding:10px;border-top:1px solid #eee;background:#fff}" +
    "textarea{flex:1;resize:none;border:1px solid #ddd;border-radius:10px;padding:9px 10px;font-size:14px;height:40px;max-height:110px;outline:none;color:#222;background:#fff}" +
    "textarea:focus{border-color:var(--c)}" +
    ".send{border:0;border-radius:10px;background:var(--c);color:#fff;padding:0 14px;font-size:14px;cursor:pointer}" +
    ".send:disabled{opacity:.5;cursor:default}" +
    ".foot{text-align:center;font-size:11px;color:#aaa;padding:0 0 8px;background:#fff}" +
    ".foot a{color:#aaa}" +
    "@media (max-width:480px){.panel{right:0;bottom:0;width:100vw;height:100%;max-height:100%;border-radius:0}.panel.open~.btn{display:none}}" +
    "</style>" +
    "<div class='panel' role='dialog'>" +
    "  <div class='head'><div class='avatar'>🤖</div><div class='who'><div class='name'></div><div class='status'></div></div><button class='x' type='button'>×</button></div>" +
    "  <div class='log' aria-live='polite'><div class='typing'><i></i><i></i><i></i></div></div>" +
    "  <div class='bar'><textarea rows='1'></textarea><button class='send' type='button'></button></div>" +
    "  <div class='foot'><a target='_blank' rel='noopener'></a></div>" +
    "</div>" +
    "<button class='btn' type='button'><svg viewBox='0 0 24 24' fill='currentColor'><path d='M12 3C6.5 3 2 6.8 2 11.5c0 2.4 1.2 4.6 3.1 6.1L4.5 21l3.8-1.9c1.1.3 2.4.5 3.7.5 5.5 0 10-3.8 10-8.5S17.5 3 12 3z'/></svg><span class='badge'></span></button>";

  function $(sel) { return root.querySelector(sel); }
  var panel = $(".panel"), btn = $(".btn"), badge = $(".badge"), log = $(".log"),
      typing = $(".typing"), input = $("textarea"), sendBtn = $(".send"), foot = $(".foot a");
  host.style.setProperty("--c", color);
  var welcomeBubble = null;
  function label() {
    $(".name").textContent = cfg.title || T.title;
    $(".status").textContent = T.status;
    $(".x").setAttribute("aria-label", T.close);
    btn.setAttribute("aria-label", T.open);
    input.placeholder = T.placeholder;
    sendBtn.textContent = T.send;
    foot.textContent = T.powered;
    if (welcomeBubble && !cfg.welcome) welcomeBubble.lastChild.nodeValue = T.welcome;
  }
  label();
  foot.href = base + "/";

  var seen = {}, cursor = 0, loaded = false, busy = false, unread = 0, pollTimer = null, hasHistory = false;

  function add(role, text, id, quiet) {
    if (id != null) {
      if (seen[id]) return;
      seen[id] = true;
    }
    var div = document.createElement("div");
    div.className = "m " + (role === "customer" ? "me" : role === "owner" ? "owner" : role === "err" ? "err" : "bot");
    if (role === "owner") {
      var tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = T.owner;
      div.appendChild(tag);
    }
    div.appendChild(document.createTextNode(text));
    log.insertBefore(div, typing);
    if (quiet && id == null && !welcomeBubble) welcomeBubble = div;
    log.scrollTop = log.scrollHeight;
    if (!quiet && !panel.classList.contains("open") && role !== "customer" && role !== "err") {
      unread++;
      badge.textContent = unread;
      badge.style.display = "block";
    }
  }

  function api(path, opts) {
    return fetch(base + path, opts).then(function (r) {
      return r.json().then(function (data) { data.__status = r.status; return data; });
    });
  }

  function poll() {
    return api("/api/messages?v=" + encodeURIComponent(visitor) + "&after=" + cursor).then(function (data) {
      var first = !loaded;
      loaded = true;
      (data.messages || []).forEach(function (m) {
        cursor = Math.max(cursor, m.id);
        hasHistory = true;
        // The visitor's own messages are already on screen, except on first load.
        if (m.role === "customer" && !first) { seen[m.id] = true; return; }
        add(m.role, m.text, m.id, first);
      });
    }).catch(function () {});
  }

  function schedule() {
    clearTimeout(pollTimer);
    var open = panel.classList.contains("open");
    if (!open && !hasHistory) return;
    pollTimer = setTimeout(function () {
      if (document.hidden || busy) { schedule(); return; }
      poll().then(schedule);
    }, open ? 3000 : 20000);
  }

  function setOpen(open) {
    panel.classList.toggle("open", open);
    if (open) {
      unread = 0;
      badge.style.display = "none";
      if (!loaded) poll().then(schedule);
      else schedule();
      setTimeout(function () { input.focus(); }, 50);
    } else {
      schedule();
    }
  }

  function send() {
    var text = input.value.trim();
    if (!text || busy) return;
    busy = true;
    sendBtn.disabled = true;
    input.value = "";
    input.style.height = "";
    add("customer", text);
    hasHistory = true;
    typing.classList.add("on");
    log.scrollTop = log.scrollHeight;
    api("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ v: visitor, text: text.slice(0, 1000), lang: langTag })
    }).then(function (data) {
      if (data.__status === 429) add("err", T.slow);
      else if (data.__status >= 400) add("err", T.error);
      else if (data.reply) add("bot", data.reply, data.reply_id);
    }).catch(function () {
      add("err", T.error);
    }).then(function () {
      busy = false;
      sendBtn.disabled = false;
      typing.classList.remove("on");
      poll().then(schedule);
    });
  }

  window.aiChatOpen = function () { setOpen(true); };
  // A page can offer ready-made questions: open the window and send one.
  window.aiChatAsk = function (text) {
    setOpen(true);
    if (busy || !text) return;
    input.value = String(text).slice(0, 1000);
    send();
  };
  // A page with its own language switch keeps the chat window in step.
  window.aiChatSetLang = function (tag) {
    langTag = tag;
    lang = pickLang(tag);
    T = TEXTS[lang];
    label();
  };
  btn.addEventListener("click", function () { setOpen(!panel.classList.contains("open")); });
  $(".x").addEventListener("click", function () { setOpen(false); });
  sendBtn.addEventListener("click", send);
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  input.addEventListener("input", function () {
    input.style.height = "";
    input.style.height = Math.min(input.scrollHeight, 110) + "px";
  });

  function mount() {
    document.body.appendChild(host);
    add("bot", cfg.welcome || T.welcome, null, true);
    // A returning visitor sees replies that came in while they were away.
    poll().then(function () {
      if (cfg.open === "true") setOpen(true);
      else schedule();
    });
  }
  if (document.body) mount();
  else document.addEventListener("DOMContentLoaded", mount);
})();
