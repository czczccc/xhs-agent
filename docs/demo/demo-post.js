// 演示专用界面：输码页换成「选一家示例店」、一键填入这家店的真实需求、预填模拟店主的回答、
// 结果页如实标注回放、用访客自己的照片时在浏览器里画封面（和服务端 cover.py 同样的三种样式）。
(() => {
  const D = window.DEMO;
  document.head.insertAdjacentHTML('beforeend', `<style>
    .demo-bar { max-width: 480px; margin: 0 auto; padding: calc(8px + env(safe-area-inset-top)) 16px 0; font-size: 12px; color: var(--sub); display: flex; justify-content: space-between; }
    .demo-bar a { color: var(--red); text-decoration: none; }
    .shop-pick { display: grid; gap: 10px; }
    .shop-pick button { text-align: left; background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 14px 16px; color: var(--text); box-shadow: var(--shadow); }
    .shop-pick b { font-size: 16px; } .shop-pick span { color: var(--sub); font-size: 13px; display: block; margin-top: 2px; }
    .demo-tip { font-size: 12px; color: var(--sub); margin: -4px 2px 12px; line-height: 1.5; }
  </style>`);
  document.body.insertAdjacentHTML('afterbegin',
    '<div class="demo-bar"><span>演示版 · 回放真实生成结果，不调用模型</span><a href="../">项目说明 →</a></div>');

  // ---------- 输码页 → 选示例店铺 ----------
  vCode = () => {
    const shops = D.data ? D.data.shops : [];
    return `<div class="screen"><div class="top"><h1>店铺笔记助手</h1>
      <p>选一家示例店铺体验完整流程：建档 → 说今天发什么 → 回答几个问题 → 拿到图文。店铺都是虚构的，文案是模型真实生成的。</p></div>
      <div class="body"><div class="shop-pick">${shops.map(s => `<button data-code="${s.code}"><b>${esc(s.profile.name)}</b>
        <span>${esc(s.profile.shop_type)} · ${esc(s.profile.city)}${esc(s.profile.area || '')} · 人均 ${s.profile.avg_price} 元</span></button>`).join('')}</div>
        ${shops.length ? '' : '<p class="muted">加载中…</p>'}</div></div>`;
  };
  bindCode = () => {
    if (!D.data) { setTimeout(render, 300); return; }
    document.querySelectorAll('[data-code]').forEach(b => b.onclick = () => { S.code = b.dataset.code; store.set('xhs_code', S.code); loadShop(); });
  };

  // ---------- 每屏渲染后的演示补充 ----------
  const shopData = () => D.data && D.data.shops.find(s => s.code === S.code);
  const baseRender = render;
  render = function () {
    baseRender();
    const shop = shopData();
    if (S.screen === 'compose' && shop) {
      document.querySelector('.shopbar').insertAdjacentHTML('afterend', `<div class="card"><div class="lbl">试试这家店的真实需求（一键填入）</div>
        <div class="chips">${shop.cases.map((c, i) => `<button class="chip" data-case="${i}">${esc(c.content_type)}：${esc(c.topic)}</button>`).join('')}</div>
        <div class="hint">也可以自己写一句；演示版会展示这家店同类型的真实生成结果。</div></div>`);
      document.querySelectorAll('[data-case]').forEach(b => b.onclick = () => {
        const c = shop.cases[+b.dataset.case];
        Object.assign(S.compose, { type: c.content_type, topic: c.topic, extra: c.extra });
        render();
      });
      const edit = document.querySelector('#edit-shop');
      edit.textContent = '换一家';
      edit.onclick = () => { store.del('xhs_code'); S.code = null; S.shop = null; go('code'); };
    }
    if (S.screen === 'questions' && D.current) {
      const qa = D.current.c.questions;
      document.querySelectorAll('[data-ans]').forEach(t => {
        const i = +t.dataset.ans;
        if (!t.value && qa[i]) { t.value = qa[i].a; S.answers[i] = qa[i].a; }
      });
      document.querySelector('.mic').insertAdjacentHTML('afterend',
        '<div class="demo-tip">问题是 Agent 真实生成的；已预填评测里「模拟店主」的回答，可以改或清空。跳过则展示不带店主细节的版本。</div>');
    }
    if (S.screen === 'result' && S.run && S.run.demo) {
      const { note, judge } = S.run.demo;
      const jt = judge && judge.hook ? `评测裁判打分：吸引力 ${judge.hook} · 相关性 ${judge.relevance} · 想去店里 ${judge.appeal} · 像店主 ${judge.tone}（满分 5，严格档）` : '';
      if (note || jt) document.querySelector('.body').insertAdjacentHTML('afterbegin',
        `<div class="demo-tip" style="margin:0 2px 10px">${esc(note)}${note && jt ? '<br>' : ''}${esc(jt)}</div>`);
    }
  };

  // ---------- 封面：有访客照片时用 canvas 画三种样式 ----------
  const W = 1080, H = 1440, PAD = 72, RED = '#FF2442';
  const FONT = '"PingFang SC","HarmonyOS Sans SC","MiSans","Microsoft YaHei",sans-serif';
  function wrap(ctx, text, maxW) {
    const lines = [];
    for (const part of text.split(/\s+/).filter(Boolean)) {
      let line = '';
      for (const ch of part) { if (line && ctx.measureText(line + ch).width > maxW) { lines.push(line); line = ch; } else line += ch; }
      if (line) lines.push(line);
    }
    return lines;
  }
  function fit(ctx, text, maxW, maxLines, size, min) {
    for (; ; size -= 6) {
      ctx.font = `900 ${size}px ${FONT}`;
      let lines = wrap(ctx, text, maxW);
      if (lines.length > 1 && !/\s/.test(text)) {  // 行长拉平，避免孤字
        let w = maxW; while (w > 100 && wrap(ctx, text, w - 10).length === lines.length) w -= 10;
        lines = wrap(ctx, text, w);
      }
      if (lines.length <= maxLines || size <= min) return { size, lines: lines.slice(0, maxLines) };
    }
  }
  function grad(ctx, y, h, a0, a1) {
    const g = ctx.createLinearGradient(0, y, 0, y + h);
    g.addColorStop(0, `rgba(0,0,0,${a0})`); g.addColorStop(1, `rgba(0,0,0,${a1})`);
    ctx.fillStyle = g; ctx.fillRect(0, y, W, h);
  }
  function drawCover(img, style, text, sub) {
    const cv = document.createElement('canvas'); cv.width = W; cv.height = H;
    const ctx = cv.getContext('2d');
    const k = Math.max(W / img.width, H / img.height), w = img.width * k, h = img.height * k;
    ctx.drawImage(img, (W - w) / 2, (H - h) * 0.45, w, h);
    ctx.textBaseline = 'top';
    const lines = (f, x, y, color, shadow) => {
      ctx.fillStyle = color; ctx.shadowColor = shadow ? 'rgba(0,0,0,.55)' : 'transparent'; ctx.shadowBlur = shadow ? 20 : 0; ctx.shadowOffsetY = shadow ? 5 : 0;
      f.lines.forEach((l, i) => ctx.fillText(l, x, y + i * f.size * 1.18));
      ctx.shadowColor = 'transparent';
      return y + f.lines.length * f.size * 1.18;
    };
    const small = (y, color) => { ctx.font = `700 44px ${FONT}`; ctx.fillStyle = color; ctx.fillText(sub, PAD, y); };
    if (style === 'bottom') {
      const f = fit(ctx, text, W - 2 * PAD, 2, 108, 70);
      const top = H - (64 + 12 + 34 + f.lines.length * f.size * 1.18 + 24 + 50 + PAD);
      ctx.fillStyle = '#fff'; ctx.fillRect(0, top, W, H - top);
      ctx.fillStyle = RED; ctx.fillRect(PAD, top + 64, 90, 12);
      const y = lines(f, PAD, top + 110, '#1C1C1E', false);
      small(y + 24, '#6E6E73');
    } else if (style === 'badge') {
      grad(ctx, H - 300, 300, 0, 0.67);
      const f = fit(ctx, text, W * 0.62, 2, 84, 56);
      const bw = Math.max(...f.lines.map(l => ctx.measureText(l).width)) + 64, bh = f.lines.length * f.size * 1.18 + 44;
      ctx.fillStyle = RED; ctx.beginPath(); ctx.roundRect(PAD, PAD, bw, bh, 24); ctx.fill();
      lines(f, PAD + 32, PAD + 18, '#fff', false);
      small(H - PAD - 60, 'rgba(255,255,255,.92)');
    } else {
      grad(ctx, 0, 620, 0.67, 0); grad(ctx, H - 360, 360, 0, 0.75);
      lines(fit(ctx, text, W - 2 * PAD, 3, 150, 90), PAD, PAD + 20, '#fff', true);
      small(H - PAD - 60, 'rgba(255,255,255,.92)');
    }
    return new Promise(r => cv.toBlob(b => r(URL.createObjectURL(b)), 'image/jpeg', 0.9));
  }
  const cache = {};
  coverUrl = () => {
    const run = S.run;
    if (!run.photos || !run.photos.length) return run.cover_url;
    const key = run.run_id + run.draft.title + S.coverStyle;
    if (cache[key]) return cache[key];
    const img = new Image();
    img.onload = async () => { cache[key] = await drawCover(img, S.coverStyle, run.demo.cover_text, run.demo.subtitle); if (S.screen === 'result') render(); };
    img.src = run.photos[0];
    return 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';  // 画好前先占位
  };

  render();
})();
