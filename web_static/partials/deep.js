(function () {
  "use strict";
  var root = document.documentElement;
  // "en": English only; "zh": Traditional Chinese lines too; "auto" (the homepage): Chinese for Chinese browsers,
  // English for everyone else, ?lang=en / ?lang=zh to choose
  var q = location.search, mode = window.DEEP_LANG || "auto";
  var english = mode === "en" || (mode === "auto" && (/[?&]lang=en\b/.test(q) ||
                (!/[?&]lang=zh\b/.test(q) && !/^zh/i.test(navigator.language || ""))));
  window.deepEnglish = english;
  if (english) {
    root.classList.add("en");
    [].forEach.call(document.querySelectorAll("[data-en]"), function (a) { a.setAttribute("href", a.getAttribute("data-en")); });
  }
  var w = document.getElementById("chat-widget");
  if (w) w.setAttribute("data-lang", english ? "en" : "zh-TW");

  // ---- page 1: the whale video, or the drawn deep sea until there is one ----
  var video = document.getElementById("hero-video"), canvas = document.getElementById("deep");
  var src = video.getAttribute("data-src");
  var drawing = true;
  if (src) {
    video.addEventListener("loadeddata", function () { video.classList.add("ready"); drawing = false; canvas.style.display = "none"; });
    video.addEventListener("error", function () { video.removeAttribute("src"); });
    video.src = src;
  }
  if ("IntersectionObserver" in window) {
    new IntersectionObserver(function (e) {
      if (e[0].isIntersecting) { if (video.getAttribute("src")) { var p = video.play(); if (p && p.catch) p.catch(function () {}); } }
      else video.pause();
    }, { threshold: 0.3 }).observe(document.getElementById("page1"));
  } else if (src) video.play();

  // the deep sea: light from far above, drifting specks, a ball of silver fish, and every ten
  // seconds something enormous coming out of the dark
  var ctx = canvas.getContext("2d"), W = 0, H = 0, DPR = Math.min(2, window.devicePixelRatio || 1);
  var fish = [], specks = [], t0 = performance.now();
  function size() {
    W = canvas.clientWidth; H = canvas.clientHeight;
    canvas.width = W * DPR; canvas.height = H * DPR; ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  }
  size(); addEventListener("resize", size);
  for (var i = 0; i < 420; i++) fish.push({ a: Math.random() * 6.283, r: Math.pow(Math.random(), 0.6), s: 0.4 + Math.random() * 0.8, z: Math.random(), vx: 0, vy: 0, x: 0, y: 0, free: 0 });
  for (i = 0; i < 90; i++) specks.push({ x: Math.random(), y: Math.random(), r: 0.5 + Math.random() * 1.6, s: 0.002 + Math.random() * 0.006, h: Math.random() < 0.6 ? 190 : 160 });
  function whale(x, y, L, alpha) {
    ctx.save(); ctx.globalAlpha = alpha; ctx.translate(x, y);
    var g = ctx.createLinearGradient(0, -L * 0.12, 0, L * 0.14);
    g.addColorStop(0, "#1a2630"); g.addColorStop(0.5, "#0b1218"); g.addColorStop(1, "#05080b");
    ctx.fillStyle = g; ctx.filter = "blur(" + Math.max(1, L / 260) + "px)";
    ctx.beginPath();
    ctx.moveTo(L * 0.50, -L * 0.10); ctx.lineTo(L * 0.56, -L * 0.04);          // the square head
    ctx.lineTo(L * 0.56, L * 0.03); ctx.quadraticCurveTo(L * 0.30, L * 0.13, -L * 0.15, L * 0.07);
    ctx.quadraticCurveTo(-L * 0.38, L * 0.03, -L * 0.46, 0);                    // tail stock
    ctx.lineTo(-L * 0.58, -L * 0.06); ctx.lineTo(-L * 0.52, 0.0); ctx.lineTo(-L * 0.58, L * 0.06); ctx.lineTo(-L * 0.46, L * 0.01);
    ctx.quadraticCurveTo(-L * 0.20, -L * 0.07, L * 0.10, -L * 0.11);
    ctx.quadraticCurveTo(L * 0.40, -L * 0.13, L * 0.50, -L * 0.10); ctx.fill();
    ctx.filter = "none"; ctx.strokeStyle = "rgba(170,210,240,0.12)"; ctx.lineWidth = 1.2;            // jaw line
    ctx.beginPath(); ctx.moveTo(L * 0.55, L * 0.025); ctx.lineTo(L * 0.18, L * 0.055); ctx.stroke();
    ctx.restore();
  }
  function frame(now) {
    if (!drawing) return;
    var t = (now - t0) / 1000, cyc = t % 10, cx = W * 0.5, cy = H * 0.52, R = Math.min(W, H) * 0.16;
    ctx.fillStyle = "#010307"; ctx.fillRect(0, 0, W, H);
    for (var k = 0; k < 4; k++) {                                                 // light shafts
      var lx = W * (0.25 + k * 0.18) + Math.sin(t * 0.2 + k) * 30;
      var sg = ctx.createLinearGradient(lx, 0, lx + 80, H);
      sg.addColorStop(0, "rgba(120,170,210,0.10)"); sg.addColorStop(1, "rgba(120,170,210,0)");
      ctx.fillStyle = sg; ctx.beginPath(); ctx.moveTo(lx - 10, 0); ctx.lineTo(lx + 22, 0); ctx.lineTo(lx + 160, H); ctx.lineTo(lx + 60, H); ctx.fill();
    }
    specks.forEach(function (p) {                                                 // fluorescent specks
      p.y -= p.s * 0.2; p.x += Math.sin(t * 0.5 + p.y * 9) * 0.0004; if (p.y < 0) p.y = 1;
      ctx.fillStyle = "hsla(" + p.h + ",90%,70%," + (0.25 + 0.35 * Math.sin(t * 2 + p.x * 40)) + ")";
      ctx.beginPath(); ctx.arc(p.x * W, p.y * H, p.r, 0, 6.283); ctx.fill();
    });
    var wx = -W * 0.6 + (cyc - 2) / 6 * W * 2.1, L = Math.max(W, H) * 0.95;      // the giant, from 2s to 8s
    var near = cyc > 2 && cyc < 8;
    fish.forEach(function (f) {
      var hx = cx + Math.cos(f.a + t * f.s * 0.6) * R * f.r, hy = cy + Math.sin(f.a * 1.3 + t * f.s * 0.6) * R * 0.75 * f.r;
      if (!f.x) { f.x = hx; f.y = hy; }
      if (near && Math.abs(f.x - (wx + L * 0.5)) < L * 0.22 && Math.abs(f.y - cy) < L * 0.16) {
        var dx = f.x - (wx + L * 0.4), dy = f.y - cy, d = Math.sqrt(dx * dx + dy * dy) + 1;
        f.vx += dx / d * 9; f.vy += dy / d * 9; f.free = 1;                        // scatter
      }
      if (f.free) { f.x += f.vx; f.y += f.vy; f.vx *= 0.94; f.vy *= 0.94; if (cyc > 8.6 || cyc < 2) { f.x += (hx - f.x) * 0.02; f.y += (hy - f.y) * 0.02; if (Math.abs(hx - f.x) < 2) f.free = 0; } }
      else { f.x += (hx - f.x) * 0.08; f.y += (hy - f.y) * 0.08; }
      ctx.fillStyle = "rgba(205,225,240," + (0.35 + 0.55 * f.z) + ")";
      ctx.save(); ctx.translate(f.x, f.y); ctx.rotate(f.a + t * f.s); ctx.fillRect(-2.2, -0.6, 4.4, 1.2); ctx.restore();
    });
    if (near) whale(wx + L * 0.5, cy + Math.sin(t) * 6, L, Math.min(1, (cyc - 2) * 1.5, (8 - cyc) * 1.5));
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);

  // ---- page 2: the cursor wakes what is under the dead wood ----
  var page2 = document.getElementById("page2"), mask = document.getElementById("mask");
  function move(x, y) {
    var rect = page2.getBoundingClientRect();
    mask.style.setProperty("--mx", (x - rect.left) + "px");
    mask.style.setProperty("--my", (y - rect.top) + "px");
  }
  page2.addEventListener("mousemove", function (e) { move(e.clientX, e.clientY); });
  page2.addEventListener("touchmove", function (e) { if (e.touches[0]) move(e.touches[0].clientX, e.touches[0].clientY); }, { passive: true });

  var dots = document.getElementById("dots");
  for (i = 0; i < 40; i++) {
    var d = document.createElement("i"), s = 1 + Math.random() * 2.5;
    d.style.cssText = "width:" + s + "px;height:" + s + "px;left:" + (Math.random() * 100) + "%;bottom:" + (Math.random() * 30 - 10) + "%;" +
      "animation-duration:" + (16 + Math.random() * 12) + "s;animation-delay:" + (Math.random() * 30) + "s;filter:opacity(" + (0.25 + Math.random() * 0.5) + ")";
    dots.appendChild(d);
  }
  var title = document.getElementById("art-title"), text = title.textContent;
  title.textContent = "";
  for (i = 0; i < text.length; i++) {
    var c = document.createElement("span");
    c.textContent = text[i] === " " ? " " : text[i];
    c.style.animationDelay = (i * 0.12) + "s";
    title.appendChild(c);
  }

  // ---- the two pages hand over as you scroll ----
  var ticking = false;
  function onScroll() {
    var vh = innerHeight, sy = scrollY || pageYOffset;
    var p = Math.max(0, Math.min(1, sy / vh));
    var p1fade = Math.max(0, 1 - Math.max(0, p - 0.05) / 0.5);
    var p2fade = Math.max(0, Math.min(1, (p - 0.45) / 0.4));
    root.style.setProperty("--p1fade", p1fade.toFixed(3));
    root.style.setProperty("--p2fade", p2fade.toFixed(3));
    ticking = false;
  }
  addEventListener("scroll", function () { if (!ticking) { ticking = true; requestAnimationFrame(onScroll); } }, { passive: true });
  onScroll();

  function easeInOutCubic(x) { return x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2; }
  document.getElementById("explore").addEventListener("click", function () {
    var from = scrollY, start = performance.now();
    root.style.scrollBehavior = "auto";
    (function step(now) {
      var k = Math.min(1, (now - start) / 1400);
      scrollTo(0, from * (1 - easeInOutCubic(k)));
      if (k < 1) requestAnimationFrame(step); else root.style.scrollBehavior = "";
    })(start);
  });
})();
