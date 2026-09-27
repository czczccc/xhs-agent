-- 由 `xhs-db init` 执行。vector(1024) 会被替换成 embedding 接口实际返回的维度。
CREATE EXTENSION IF NOT EXISTS vector;

-- 参考笔记库：用于检索相似爆款笔记，给规划节点提供风格和结构参考
CREATE TABLE IF NOT EXISTS notes (
    id          BIGSERIAL PRIMARY KEY,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    tags        TEXT[] NOT NULL DEFAULT '{}',
    category    TEXT,
    likes       INT DEFAULT 0,
    embedding   vector(1024),
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- 餐饮商家场景新增字段（ADD COLUMN IF NOT EXISTS：老库重新 init 即可升级）
ALTER TABLE notes ADD COLUMN IF NOT EXISTS collects     INT;
ALTER TABLE notes ADD COLUMN IF NOT EXISTS comments     INT;
ALTER TABLE notes ADD COLUMN IF NOT EXISTS shop_type    TEXT;   -- 火锅 / 咖啡 / 家常菜 …
ALTER TABLE notes ADD COLUMN IF NOT EXISTS content_type TEXT;   -- 探店 / 上新 / 活动 / 日常 / 老板故事 / 攻略
ALTER TABLE notes ADD COLUMN IF NOT EXISTS author_type  TEXT;   -- 商家 / 博主 / 用户
ALTER TABLE notes ADD COLUMN IF NOT EXISTS city         TEXT;

-- 按标题去重，重复导入时更新而不是插入
CREATE UNIQUE INDEX IF NOT EXISTS notes_title_key ON notes (title);

CREATE INDEX IF NOT EXISTS notes_embedding_idx
    ON notes USING hnsw (embedding vector_cosine_ops);

-- 每次生成的运行记录，便于复盘和评测
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    topic       TEXT NOT NULL,
    status      TEXT NOT NULL,          -- pending_review / approved / rejected / failed / error
    result      JSONB,
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);

-- 店主反馈和使用行为（评分 / 复制 / 保存图片 / 换封面样式），见 feedback.py
CREATE TABLE IF NOT EXISTS events (
    id            BIGSERIAL PRIMARY KEY,
    run_id        TEXT NOT NULL,
    shop_code     TEXT,                 -- 阶段 4 试用码
    shop_name     TEXT,
    content_type  TEXT,
    kind          TEXT NOT NULL,        -- rating / copy_title / copy_body / save_images / cover_style
    rating        TEXT,                 -- ready / edit / bad
    comment       TEXT,
    value         TEXT,
    created_at    TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS events_run_idx ON events (run_id);

-- 试用码：每家试用店一个码，档案存服务端，按天限额（见 shops.py）
CREATE TABLE IF NOT EXISTS shops (
    code         TEXT PRIMARY KEY,
    label        TEXT NOT NULL DEFAULT '',
    profile      JSONB,
    daily_limit  INT NOT NULL DEFAULT 20,
    active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ DEFAULT now(),
    updated_at   TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS shop_usage (
    code   TEXT NOT NULL REFERENCES shops(code),
    day    DATE NOT NULL,
    count  INT NOT NULL DEFAULT 0,
    PRIMARY KEY (code, day)
);
ALTER TABLE runs ADD COLUMN IF NOT EXISTS shop_code TEXT;
