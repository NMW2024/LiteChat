/**
 * LiteChat Server
 * 轻量聊天服务器 - 只做路由和离线缓存
 * 资源限制: 1000MHz CPU, 1024MB RAM
 */

const http = require('http');
const crypto = require('crypto');
const { WebSocketServer, WebSocket } = require('ws');
const Database = require('better-sqlite3');
const path = require('path');
const fs = require('fs');

// ── 配置 ──────────────────────────────────────────────
const PORT = parseInt(process.env.PORT || '8080');
const JWT_SECRET = process.env.JWT_SECRET || 'change-me-in-production-' + crypto.randomBytes(16).toString('hex');
const DB_PATH = process.env.DB_PATH || '/data/chat.db';
const MAX_OFFLINE_MSG = parseInt(process.env.MAX_OFFLINE_MSG || '500');  // 每用户最大离线消息
const OFFLINE_MSG_TTL = parseInt(process.env.OFFLINE_MSG_TTL || '7');    // 离线消息保留天数
const BCRYPT_ROUNDS = 10;

console.log(`[Config] PORT=${PORT} DB=${DB_PATH} MAX_OFFLINE=${MAX_OFFLINE_MSG} TTL=${OFFLINE_MSG_TTL}d`);

// ── 数据库初始化 ───────────────────────────────────────
const dbDir = path.dirname(DB_PATH);
if (!fs.existsSync(dbDir)) fs.mkdirSync(dbDir, { recursive: true });

const db = new Database(DB_PATH);
db.pragma('journal_mode = WAL');
db.pragma('synchronous = NORMAL');
db.pragma('cache_size = -8000');   // 8MB cache
db.pragma('temp_store = memory');

db.exec(`
  CREATE TABLE IF NOT EXISTS users (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    username  TEXT    UNIQUE NOT NULL,
    password  TEXT    NOT NULL,           -- bcrypt hash
    created_at INTEGER DEFAULT (unixepoch())
  );

  CREATE TABLE IF NOT EXISTS offline_msgs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    to_user    TEXT    NOT NULL,
    from_user  TEXT    NOT NULL,
    msg_id     TEXT    NOT NULL,          -- 客户端生成的UUID
    payload    TEXT    NOT NULL,          -- JSON: {type, content, timestamp, ...}
    created_at INTEGER DEFAULT (unixepoch()),
    UNIQUE(msg_id)
  );

  CREATE INDEX IF NOT EXISTS idx_offline_to ON offline_msgs(to_user, created_at);

  -- 群组
  CREATE TABLE IF NOT EXISTS groups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id   TEXT    UNIQUE NOT NULL,
    name       TEXT    NOT NULL,
    owner      TEXT    NOT NULL,
    created_at INTEGER DEFAULT (unixepoch())
  );

  CREATE TABLE IF NOT EXISTS group_members (
    group_id  TEXT NOT NULL,
    username  TEXT NOT NULL,
    role      TEXT DEFAULT 'member',      -- owner/admin/member
    joined_at INTEGER DEFAULT (unixepoch()),
    PRIMARY KEY (group_id, username)
  );
`);

// 定期清理过期离线消息
const cleanupStmt = db.prepare(`DELETE FROM offline_msgs WHERE created_at < unixepoch() - ?`);
setInterval(() => {
  const deleted = cleanupStmt.run(OFFLINE_MSG_TTL * 86400);
  if (deleted.changes > 0) console.log(`[Cleanup] Deleted ${deleted.changes} expired offline messages`);
}, 3600_000); // 每小时

// ── 简单JWT（不依赖外部库）─────────────────────────────
function base64url(buf) {
  return Buffer.from(buf).toString('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');
}
function signJWT(payload) {
  const header = base64url(JSON.stringify({ alg: 'HS256', typ: 'JWT' }));
  const body = base64url(JSON.stringify(payload));
  const sig = base64url(crypto.createHmac('sha256', JWT_SECRET).update(`${header}.${body}`).digest());
  return `${header}.${body}.${sig}`;
}
function verifyJWT(token) {
  try {
    const parts = token.split('.');
    if (parts.length !== 3) return null;
    const expectedSig = base64url(crypto.createHmac('sha256', JWT_SECRET).update(`${parts[0]}.${parts[1]}`).digest());
    if (parts[2] !== expectedSig) return null;
    const payload = JSON.parse(Buffer.from(parts[1], 'base64').toString());
    if (payload.exp && Date.now() / 1000 > payload.exp) return null;
    return payload;
  } catch { return null; }
}

// ── 密码哈希（纯Node，不依赖bcrypt）──────────────────
function hashPassword(password) {
  const salt = crypto.randomBytes(16).toString('hex');
  const hash = crypto.scryptSync(password, salt, 64).toString('hex');
  return `${salt}:${hash}`;
}
function verifyPassword(password, stored) {
  const [salt, hash] = stored.split(':');
  const testHash = crypto.scryptSync(password, salt, 64).toString('hex');
  return crypto.timingSafeEqual(Buffer.from(hash, 'hex'), Buffer.from(testHash, 'hex'));
}

// ── HTTP 路由 ─────────────────────────────────────────
const httpServer = http.createServer((req, res) => {
  res.setHeader('Content-Type', 'application/json');
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');

  if (req.method === 'OPTIONS') { res.writeHead(204); return res.end(); }

  const url = new URL(req.url, `http://localhost`);
  const route = `${req.method} ${url.pathname}`;

  // 读取body
  const readBody = () => new Promise((resolve, reject) => {
    let data = '';
    req.on('data', c => { data += c; if (data.length > 65536) reject(new Error('Too large')); });
    req.on('end', () => { try { resolve(JSON.parse(data || '{}')); } catch { resolve({}); } });
    req.on('error', reject);
  });

  const send = (status, obj) => { res.writeHead(status); res.end(JSON.stringify(obj)); };
  const authUser = () => {
    const auth = req.headers.authorization || '';
    if (!auth.startsWith('Bearer ')) return null;
    return verifyJWT(auth.slice(7));
  };

  (async () => {
    try {
      if (route === 'POST /api/register') {
        const { username, password } = await readBody();
        if (!username || !password) return send(400, { error: 'username and password required' });
        if (!/^[a-zA-Z0-9_]{3,32}$/.test(username)) return send(400, { error: 'Invalid username (3-32 alphanumeric/_)' });
        if (password.length < 6) return send(400, { error: 'Password too short' });
        try {
          db.prepare('INSERT INTO users (username, password) VALUES (?, ?)').run(username, hashPassword(password));
          send(201, { ok: true });
        } catch (e) {
          if (e.message.includes('UNIQUE')) return send(409, { error: 'Username taken' });
          throw e;
        }
      }

      else if (route === 'POST /api/login') {
        const { username, password } = await readBody();
        const user = db.prepare('SELECT * FROM users WHERE username = ?').get(username);
        if (!user || !verifyPassword(password, user.password)) return send(401, { error: 'Invalid credentials' });
        const token = signJWT({ sub: username, exp: Math.floor(Date.now() / 1000) + 86400 * 30 });
        send(200, { token, username });
      }

      else if (route === 'GET /api/user/search') {
        const claim = authUser(); if (!claim) return send(401, { error: 'Unauthorized' });
        const q = url.searchParams.get('q') || '';
        if (q.length < 2) return send(400, { error: 'Query too short' });
        const users = db.prepare("SELECT username FROM users WHERE username LIKE ? AND username != ? LIMIT 20")
          .all(`%${q}%`, claim.sub).map(u => u.username);
        send(200, { users });
      }

      else if (route === 'GET /api/health') {
        const userCount = db.prepare('SELECT COUNT(*) as c FROM users').get().c;
        const offlineCount = db.prepare('SELECT COUNT(*) as c FROM offline_msgs').get().c;
        send(200, { ok: true, users: userCount, offline_msgs: offlineCount, online: onlineUsers.size });
      }

      // 群组API
      else if (route === 'POST /api/group/create') {
        const claim = authUser(); if (!claim) return send(401, { error: 'Unauthorized' });
        const { name } = await readBody();
        if (!name || name.length < 2) return send(400, { error: 'Group name too short' });
        const groupId = 'g_' + crypto.randomBytes(8).toString('hex');
        db.prepare('INSERT INTO groups (group_id, name, owner) VALUES (?, ?, ?)').run(groupId, name, claim.sub);
        db.prepare('INSERT INTO group_members (group_id, username, role) VALUES (?, ?, ?)').run(groupId, claim.sub, 'owner');
        send(201, { group_id: groupId, name });
      }

      else if (route === 'POST /api/group/join') {
        const claim = authUser(); if (!claim) return send(401, { error: 'Unauthorized' });
        const { group_id } = await readBody();
        const group = db.prepare('SELECT * FROM groups WHERE group_id = ?').get(group_id);
        if (!group) return send(404, { error: 'Group not found' });
        try {
          db.prepare('INSERT INTO group_members (group_id, username) VALUES (?, ?)').run(group_id, claim.sub);
        } catch (e) { if (!e.message.includes('UNIQUE')) throw e; }
        send(200, { ok: true, name: group.name });
      }

      else if (route === 'GET /api/group/members') {
        const claim = authUser(); if (!claim) return send(401, { error: 'Unauthorized' });
        const groupId = url.searchParams.get('group_id');
        const members = db.prepare('SELECT username, role FROM group_members WHERE group_id = ?').all(groupId);
        send(200, { members });
      }

      else {
        send(404, { error: 'Not found' });
      }
    } catch (e) {
      console.error('[HTTP Error]', e.message);
      send(500, { error: 'Internal server error' });
    }
  })();
});

// ── WebSocket 服务 ─────────────────────────────────────
const wss = new WebSocketServer({ server: httpServer });

// username -> WebSocket
const onlineUsers = new Map();

// 查询用户的群组
function getUserGroups(username) {
  return db.prepare('SELECT group_id FROM group_members WHERE username = ?').all(username).map(r => r.group_id);
}

// 获取群成员
function getGroupMembers(groupId) {
  return db.prepare('SELECT username FROM group_members WHERE group_id = ?').all(groupId).map(r => r.username);
}

// 存离线消息
const insertOffline = db.prepare(`
  INSERT OR IGNORE INTO offline_msgs (to_user, from_user, msg_id, payload)
  VALUES (?, ?, ?, ?)
`);
const countOffline = db.prepare(`SELECT COUNT(*) as c FROM offline_msgs WHERE to_user = ?`);

function storeOffline(toUser, fromUser, msgId, payload) {
  const count = countOffline.get(toUser).c;
  if (count >= MAX_OFFLINE_MSG) {
    // 删最旧的
    db.prepare('DELETE FROM offline_msgs WHERE id = (SELECT id FROM offline_msgs WHERE to_user = ? ORDER BY created_at ASC LIMIT 1)').run(toUser);
  }
  insertOffline.run(toUser, fromUser, msgId, JSON.stringify(payload));
}

// 发送给在线用户
function sendToUser(username, data) {
  const ws = onlineUsers.get(username);
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(data));
    return true;
  }
  return false;
}

wss.on('connection', (ws, req) => {
  let authedUser = null;

  // 心跳
  ws.isAlive = true;
  ws.on('pong', () => { ws.isAlive = true; });

  ws.on('message', (raw) => {
    let msg;
    try { msg = JSON.parse(raw); } catch { return ws.send(JSON.stringify({ type: 'error', message: 'Invalid JSON' })); }

    // ── 认证 ──
    if (msg.type === 'auth') {
      const claim = verifyJWT(msg.token);
      if (!claim) return ws.send(JSON.stringify({ type: 'auth_fail', message: 'Invalid token' }));

      authedUser = claim.sub;

      // 踢掉旧连接
      const oldWs = onlineUsers.get(authedUser);
      if (oldWs && oldWs !== ws && oldWs.readyState === WebSocket.OPEN) {
        oldWs.send(JSON.stringify({ type: 'kicked', message: 'Logged in from another location' }));
        oldWs.close();
      }
      onlineUsers.set(authedUser, ws);

      // 推送离线消息
      const offlineMsgs = db.prepare('SELECT * FROM offline_msgs WHERE to_user = ? ORDER BY created_at ASC').all(authedUser);
      if (offlineMsgs.length > 0) {
        ws.send(JSON.stringify({ type: 'offline_msgs', messages: offlineMsgs.map(m => JSON.parse(m.payload)) }));
        db.prepare('DELETE FROM offline_msgs WHERE to_user = ?').run(authedUser);
      }

      // 告知在线联系人
      const groups = getUserGroups(authedUser);
      const notified = new Set();
      groups.forEach(gid => {
        getGroupMembers(gid).forEach(member => {
          if (member !== authedUser && !notified.has(member)) {
            notified.add(member);
            sendToUser(member, { type: 'user_online', username: authedUser });
          }
        });
      });

      ws.send(JSON.stringify({ type: 'auth_ok', username: authedUser, groups }));
      console.log(`[WS] ${authedUser} connected (${onlineUsers.size} online)`);
      return;
    }

    if (!authedUser) return ws.send(JSON.stringify({ type: 'error', message: 'Not authenticated' }));

    // ── 私聊 ──
    if (msg.type === 'chat') {
      const { to, content, msg_id, timestamp, content_type = 'text' } = msg;
      if (!to || !content || !msg_id) return;

      const payload = { type: 'chat', from: authedUser, to, content, msg_id, timestamp: timestamp || Date.now(), content_type };

      // 回执给发送方
      ws.send(JSON.stringify({ type: 'ack', msg_id }));

      const delivered = sendToUser(to, payload);
      if (!delivered) {
        // 目标离线，存缓存
        const target = db.prepare('SELECT id FROM users WHERE username = ?').get(to);
        if (target) storeOffline(to, authedUser, msg_id, payload);
        else ws.send(JSON.stringify({ type: 'error', msg_id, message: 'User not found' }));
      }
    }

    // ── 群聊 ──
    else if (msg.type === 'group_chat') {
      const { group_id, content, msg_id, timestamp, content_type = 'text' } = msg;
      if (!group_id || !content || !msg_id) return;

      const members = getGroupMembers(group_id);
      if (!members.includes(authedUser)) return ws.send(JSON.stringify({ type: 'error', message: 'Not a member' }));

      const payload = { type: 'group_chat', from: authedUser, group_id, content, msg_id, timestamp: timestamp || Date.now(), content_type };

      ws.send(JSON.stringify({ type: 'ack', msg_id }));

      members.forEach(member => {
        if (member === authedUser) return;
        const delivered = sendToUser(member, payload);
        if (!delivered) storeOffline(member, authedUser, msg_id, payload);
      });
    }

    // ── 查询用户在线状态 ──
    else if (msg.type === 'presence') {
      const { users } = msg;
      if (!Array.isArray(users)) return;
      const result = {};
      users.forEach(u => { result[u] = onlineUsers.has(u); });
      ws.send(JSON.stringify({ type: 'presence', users: result }));
    }

    // ── 输入中提示 ──
    else if (msg.type === 'typing') {
      const { to } = msg;
      if (to) sendToUser(to, { type: 'typing', from: authedUser });
    }
  });

  ws.on('close', () => {
    if (authedUser) {
      onlineUsers.delete(authedUser);
      console.log(`[WS] ${authedUser} disconnected (${onlineUsers.size} online)`);

      // 通知联系人下线
      const groups = getUserGroups(authedUser);
      const notified = new Set();
      groups.forEach(gid => {
        getGroupMembers(gid).forEach(member => {
          if (member !== authedUser && !notified.has(member)) {
            notified.add(member);
            sendToUser(member, { type: 'user_offline', username: authedUser });
          }
        });
      });
    }
  });

  ws.on('error', (e) => console.error('[WS Error]', e.message));
});

// 心跳检测
const heartbeat = setInterval(() => {
  wss.clients.forEach(ws => {
    if (!ws.isAlive) return ws.terminate();
    ws.isAlive = false;
    ws.ping();
  });
}, 30_000);
wss.on('close', () => clearInterval(heartbeat));

// ── 启动 ──────────────────────────────────────────────
httpServer.listen(PORT, () => {
  console.log(`[Server] LiteChat running on port ${PORT}`);
  console.log(`[Server] WebSocket: ws://0.0.0.0:${PORT}`);
  console.log(`[Server] HTTP API: http://0.0.0.0:${PORT}/api`);
});

process.on('SIGTERM', () => { db.close(); process.exit(0); });
process.on('SIGINT',  () => { db.close(); process.exit(0); });
