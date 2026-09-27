// 演示模式（GitHub Pages，无后端）：拦截页面发出的 /api/* 请求，用评测里真实生成过的结果回放。
// 页面本身和线上版是同一份代码（scripts/build_demo.py 复制），只有这里和 demo-post.js 是演示专用的。
(() => {
  const DEMO = window.DEMO = { data: null, current: null, runs: {}, photos: {}, profiles: {}, used: 0 };
  const ready = fetch('data.json').then(r => r.json()).then(d => { DEMO.data = d; });
  const realFetch = window.fetch.bind(window);
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const json = (obj, status = 200) => new Response(JSON.stringify(obj), { status, headers: { 'Content-Type': 'application/json' } });
  const shopOf = opts => DEMO.data.shops.find(s => s.code === ((opts.headers || {})['X-Shop-Code'] || '').toUpperCase());

  // 选题完全一致就用那条；否则同店同类型；再不行用这家店第一条（结果页会如实提示）
  function pickCase(shop, body) {
    const exact = shop.cases.find(c => c.topic === (body.topic || '').trim());
    if (exact) return { c: exact, exact: true };
    return { c: shop.cases.find(c => c.content_type === body.content_type) || shop.cases[0], exact: false };
  }

  function view(runId, c, text, body, note) {
    const p = DEMO.profiles[runId.shop] || {};
    return {
      run_id: runId.id, status: 'pending_review', error: null,
      plan: { cover_text: c.cover_text }, draft: { title: text.title, body: text.body, tags: text.tags },
      review: { passed: true, issues: [] }, references: [], stats: {},
      cover_url: `covers/${c.id}.png`, cover_style: body.cover_style || 'big',
      photos: (body.photos || []).map(id => DEMO.photos[id]).filter(Boolean),
      demo: { note, judge: text.judge, cover_text: c.cover_text, subtitle: [p.name, p.area || p.city].filter(Boolean).join(' · ') },
    };
  }

  // 按真实流程的节奏逐个推进度事件（SSE 格式，和后端一致）
  function sse(nodes, final) {
    const enc = new TextEncoder();
    return new Response(new ReadableStream({
      async start(ctrl) {
        for (const [node, ms] of nodes) { await sleep(ms); ctrl.enqueue(enc.encode(`data: ${JSON.stringify({ type: 'node', node })}\n\n`)); }
        const run = await final();
        ctrl.enqueue(enc.encode(`data: ${JSON.stringify({ type: 'done', run })}\n\n`));
        ctrl.close();
      },
    }), { headers: { 'Content-Type': 'text/event-stream' } });
  }

  async function api(url, opts) {
    await ready;
    const shop = shopOf(opts);
    if (!shop) return json({ detail: '演示码不对，请从示例店铺里选一家' }, 401);
    const body = typeof opts.body === 'string' ? JSON.parse(opts.body) : null;
    const profile = DEMO.profiles[shop.code] || shop.profile;

    if (url === '/api/shop') {
      if (opts.method === 'PUT') DEMO.profiles[shop.code] = body;
      return json({ code: shop.code, label: shop.profile.name, profile: DEMO.profiles[shop.code] || shop.profile, daily_limit: 20, used_today: DEMO.used });
    }
    if (url === '/api/questions') {
      DEMO.current = pickCase(shop, body);
      await sleep(900);
      return json({ questions: DEMO.current.c.questions.map(q => q.q) });
    }
    if (url === '/api/uploads') {
      const id = 'p' + Math.random().toString(16).slice(2);
      DEMO.photos[id] = URL.createObjectURL(opts.body.get('file'));
      await sleep(400);
      return json({ id, url: DEMO.photos[id] });
    }
    if (url === '/api/runs/stream') {
      const { c, exact } = DEMO.current && DEMO.current.c.topic === body.topic ? DEMO.current : pickCase(shop, body);
      const answered = (body.answers || []).length > 0 && c.answered;
      const text = answered ? c.answered : c.skip;
      const note = exact ? '' : `演示版不实时调用模型：这里展示的是这家店「${c.content_type}」类的一条真实生成结果（需求：${c.topic}）。`;
      const id = { id: 'demo' + Math.random().toString(16).slice(2, 10), shop: shop.code };
      DEMO.runs[id.id] = { c, body, id };
      DEMO.used += 1;
      DEMO.profiles[shop.code] = profile;
      return sse([['retrieve', 500], ['plan', 1100], ['write', 1800], ['review', 400], ['cover', 500]],
        async () => view(id, c, text, body, note));
    }
    const m = url.match(/^\/api\/runs\/([^/]+)\/(decision\/stream|events)$/);
    if (m && m[2] === 'events') return json({ ok: true });
    if (m) {
      const r = DEMO.runs[m[1]];
      const text = r.c.revised || r.c.skip;
      DEMO.used += 1;
      return sse([['human_gate', 100], ['write', 1800], ['review', 400], ['cover', 500]], async () =>
        view(r.id, r.c, text, r.body, '演示版的「按意见重写」不实时调用模型，展示的是同一需求的另一版真实生成结果（换提示词前的版本）。'));
    }
    return json({ detail: '演示版不支持这个操作' }, 404);
  }

  window.fetch = (url, opts = {}) => (typeof url === 'string' && url.startsWith('/api/') ? api(url, opts) : realFetch(url, opts));
})();
