// ========================================
// MONOGRAM
// A minimal, real-time messenger — Telegram-inspired, not Telegram.
// One file. One command to run: node monogram.js
// ========================================

// ========================================
// CONFIGURATION
// ========================================

const express = require('express');
const http = require('http');
const crypto = require('crypto');
const path = require('path');
const sqlite3 = require('sqlite3').verbose();
const { Server } = require('socket.io');

const PORT = process.env.PORT || 3000;
const DB_PATH = path.join(__dirname, 'monogram.db');
const SESSION_COOKIE = 'mg_session';
const SESSION_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000; // 30 days
const MAX_MESSAGE_LENGTH = 4000;
const MIN_USERNAME_LENGTH = 3;
const MAX_USERNAME_LENGTH = 24;
const MIN_PASSWORD_LENGTH = 6;

// ========================================
// DATABASE
// ========================================

const db = new sqlite3.Database(DB_PATH);
db.configure('busyTimeout', 5000);

function run(sql, params) {
  return new Promise((resolve, reject) => {
    db.run(sql, params || [], function (err) {
      if (err) reject(err);
      else resolve({ lastID: this.lastID, changes: this.changes });
    });
  });
}

function get(sql, params) {
  return new Promise((resolve, reject) => {
    db.get(sql, params || [], (err, row) => {
      if (err) reject(err);
      else resolve(row);
    });
  });
}

function all(sql, params) {
  return new Promise((resolve, reject) => {
    db.all(sql, params || [], (err, rows) => {
      if (err) reject(err);
      else resolve(rows);
    });
  });
}

// ========================================
// DATABASE INITIALIZATION
// (never drops or wipes existing data — CREATE TABLE IF NOT EXISTS only)
// ========================================

function initDatabase() {
  db.serialize(() => {
    db.run('PRAGMA foreign_keys = ON');

    db.run(`
      CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        display_name TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        avatar TEXT,
        created_at INTEGER NOT NULL,
        last_seen INTEGER NOT NULL
      )
    `);

    db.run(`
      CREATE TABLE IF NOT EXISTS chats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT NOT NULL DEFAULT 'private',
        created_at INTEGER NOT NULL
      )
    `);

    db.run(`
      CREATE TABLE IF NOT EXISTS chat_members (
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        PRIMARY KEY (chat_id, user_id)
      )
    `);

    db.run(`
      CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        sender_id INTEGER NOT NULL,
        text TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        edited_at INTEGER,
        is_read INTEGER NOT NULL DEFAULT 0,
        deleted INTEGER NOT NULL DEFAULT 0
      )
    `);

    db.run('CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id)');
    db.run('CREATE INDEX IF NOT EXISTS idx_chat_members_user ON chat_members(user_id)');

    console.log('[Monogram] Database ready at ' + DB_PATH);
  });
}

// ========================================
// AUTHENTICATION
// ========================================

function hashPassword(password) {
  const salt = crypto.randomBytes(16).toString('hex');
  const hash = crypto.scryptSync(password, salt, 64).toString('hex');
  return salt + ':' + hash;
}

function verifyPassword(password, stored) {
  const parts = stored.split(':');
  if (parts.length !== 2) return false;
  const salt = parts[0];
  const hash = parts[1];
  const check = crypto.scryptSync(password, salt, 64).toString('hex');
  const a = Buffer.from(hash, 'hex');
  const b = Buffer.from(check, 'hex');
  if (a.length !== b.length) return false;
  return crypto.timingSafeEqual(a, b);
}

// In-memory session store: token -> { userId, createdAt }
// Sessions are ephemeral by design (a restart simply asks the user to log
// in again); user data itself always survives restarts via SQLite.
const sessions = new Map();

function createSession(userId) {
  const token = crypto.randomBytes(32).toString('hex');
  sessions.set(token, { userId: userId, createdAt: Date.now() });
  return token;
}

function destroySession(token) {
  sessions.delete(token);
}

function parseCookies(header) {
  const out = {};
  if (!header) return out;
  header.split(';').forEach((pair) => {
    const idx = pair.indexOf('=');
    if (idx === -1) return;
    const key = pair.slice(0, idx).trim();
    const val = pair.slice(idx + 1).trim();
    if (key) out[key] = decodeURIComponent(val);
  });
  return out;
}

function getUserIdFromRequest(req) {
  const cookies = parseCookies(req.headers.cookie);
  const token = cookies[SESSION_COOKIE];
  if (!token) return null;
  const session = sessions.get(token);
  if (!session) return null;
  return session.userId;
}

function requireAuth(req, res, next) {
  const userId = getUserIdFromRequest(req);
  if (!userId) {
    return res.status(401).json({ error: 'Not authenticated' });
  }
  req.userId = userId;
  next();
}

// ========================================
// VALIDATION HELPERS
// ========================================

function isValidUsername(username) {
  if (typeof username !== 'string') return false;
  if (username.length < MIN_USERNAME_LENGTH || username.length > MAX_USERNAME_LENGTH) return false;
  return /^[a-zA-Z0-9_]+$/.test(username);
}

function isValidDisplayName(name) {
  if (typeof name !== 'string') return false;
  const trimmed = name.trim();
  return trimmed.length >= 1 && trimmed.length <= 40;
}

async function isChatMember(chatId, userId) {
  const row = await get('SELECT 1 FROM chat_members WHERE chat_id = ? AND user_id = ?', [chatId, userId]);
  return !!row;
}

function publicUser(row) {
  if (!row) return null;
  return {
    id: row.id,
    username: row.username,
    display_name: row.display_name,
    avatar: row.avatar || null,
    last_seen: row.last_seen
  };
}

// ========================================
// EXPRESS
// ========================================

const app = express();
app.use(express.json({ limit: '1mb' }));

// ========================================
// API
// ========================================

app.post('/api/register', async (req, res) => {
  try {
    const body = req.body || {};
    const username = (body.username || '').trim();
    const displayName = (body.display_name || '').trim() || username;
    const password = body.password || '';

    if (!username) return res.status(400).json({ error: 'Username is required' });
    if (!isValidUsername(username)) {
      return res.status(400).json({ error: 'Invalid username. Use 3-24 letters, numbers, or underscores.' });
    }
    if (!isValidDisplayName(displayName)) {
      return res.status(400).json({ error: 'Invalid display name' });
    }
    if (password.length < MIN_PASSWORD_LENGTH) {
      return res.status(400).json({ error: 'Password too short' });
    }

    const existing = await get('SELECT id FROM users WHERE username = ? COLLATE NOCASE', [username]);
    if (existing) {
      return res.status(409).json({ error: 'Username already taken' });
    }

    const now = Date.now();
    const passwordHash = hashPassword(password);
    const result = await run(
      'INSERT INTO users (username, display_name, password_hash, avatar, created_at, last_seen) VALUES (?, ?, ?, ?, ?, ?)',
      [username, displayName, passwordHash, null, now, now]
    );

    const token = createSession(result.lastID);
    res.cookie ? null : null; // (no cookie-parser dependency; set header manually below)
    res.setHeader('Set-Cookie', buildCookie(SESSION_COOKIE, token));
    const user = await get('SELECT * FROM users WHERE id = ?', [result.lastID]);
    res.json({ user: publicUser(user) });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Registration failed' });
  }
});

app.post('/api/login', async (req, res) => {
  try {
    const body = req.body || {};
    const username = (body.username || '').trim();
    const password = body.password || '';

    if (!username || !password) {
      return res.status(400).json({ error: 'Username and password are required' });
    }

    const user = await get('SELECT * FROM users WHERE username = ? COLLATE NOCASE', [username]);
    if (!user || !verifyPassword(password, user.password_hash)) {
      return res.status(401).json({ error: 'Invalid username or password' });
    }

    await run('UPDATE users SET last_seen = ? WHERE id = ?', [Date.now(), user.id]);
    const token = createSession(user.id);
    res.setHeader('Set-Cookie', buildCookie(SESSION_COOKIE, token));
    res.json({ user: publicUser(user) });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Login failed' });
  }
});

app.post('/api/logout', (req, res) => {
  const cookies = parseCookies(req.headers.cookie);
  const token = cookies[SESSION_COOKIE];
  if (token) destroySession(token);
  res.setHeader('Set-Cookie', buildCookie(SESSION_COOKIE, '', true));
  res.json({ ok: true });
});

app.get('/api/me', requireAuth, async (req, res) => {
  const user = await get('SELECT * FROM users WHERE id = ?', [req.userId]);
  if (!user) return res.status(401).json({ error: 'Not authenticated' });
  res.json({ user: publicUser(user) });
});

app.put('/api/profile', requireAuth, async (req, res) => {
  try {
    const body = req.body || {};
    const updates = [];
    const params = [];

    if (body.display_name !== undefined) {
      if (!isValidDisplayName(body.display_name)) {
        return res.status(400).json({ error: 'Invalid display name' });
      }
      updates.push('display_name = ?');
      params.push(body.display_name.trim());
    }
    if (body.avatar !== undefined) {
      const avatar = (body.avatar || '').trim();
      if (avatar.length > 500) return res.status(400).json({ error: 'Avatar URL too long' });
      updates.push('avatar = ?');
      params.push(avatar || null);
    }
    if (updates.length === 0) return res.status(400).json({ error: 'Nothing to update' });

    params.push(req.userId);
    await run('UPDATE users SET ' + updates.join(', ') + ' WHERE id = ?', params);
    const user = await get('SELECT * FROM users WHERE id = ?', [req.userId]);
    res.json({ user: publicUser(user) });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Update failed' });
  }
});

app.get('/api/users/search', requireAuth, async (req, res) => {
  try {
    let q = (req.query.q || '').trim();
    if (q.startsWith('@')) q = q.slice(1);
    if (!q) return res.json({ users: [] });
    const rows = await all(
      'SELECT * FROM users WHERE username LIKE ? AND id != ? ORDER BY username LIMIT 20',
      ['%' + q + '%', req.userId]
    );
    res.json({ users: rows.map(publicUser) });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Search failed' });
  }
});

app.get('/api/chats', requireAuth, async (req, res) => {
  try {
    const chats = await all(
      `SELECT c.id, c.type, c.created_at
       FROM chats c
       JOIN chat_members cm ON cm.chat_id = c.id
       WHERE cm.user_id = ?`,
      [req.userId]
    );

    const result = [];
    for (const chat of chats) {
      const other = await get(
        `SELECT u.* FROM users u
         JOIN chat_members cm ON cm.user_id = u.id
         WHERE cm.chat_id = ? AND u.id != ?`,
        [chat.id, req.userId]
      );
      const lastMessage = await get(
        `SELECT * FROM messages WHERE chat_id = ? ORDER BY created_at DESC LIMIT 1`,
        [chat.id]
      );
      const unreadRow = await get(
        `SELECT COUNT(*) as cnt FROM messages
         WHERE chat_id = ? AND sender_id != ? AND is_read = 0 AND deleted = 0`,
        [chat.id, req.userId]
      );

      result.push({
        id: chat.id,
        type: chat.type,
        peer: publicUser(other),
        last_message: lastMessage
          ? {
              text: lastMessage.deleted ? 'Message deleted' : lastMessage.text,
              created_at: lastMessage.created_at,
              sender_id: lastMessage.sender_id,
              deleted: !!lastMessage.deleted
            }
          : null,
        unread_count: unreadRow ? unreadRow.cnt : 0
      });
    }

    result.sort((a, b) => {
      const ta = a.last_message ? a.last_message.created_at : a.id;
      const tb = b.last_message ? b.last_message.created_at : b.id;
      return tb - ta;
    });

    res.json({ chats: result });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to load chats' });
  }
});

app.post('/api/chats', requireAuth, async (req, res) => {
  try {
    const targetId = parseInt((req.body || {}).userId, 10);
    if (!targetId || targetId === req.userId) {
      return res.status(400).json({ error: 'Invalid target user' });
    }
    const targetUser = await get('SELECT * FROM users WHERE id = ?', [targetId]);
    if (!targetUser) return res.status(404).json({ error: 'User not found' });

    // Look for an existing private chat between the two users
    const existing = await get(
      `SELECT c.id FROM chats c
       JOIN chat_members m1 ON m1.chat_id = c.id AND m1.user_id = ?
       JOIN chat_members m2 ON m2.chat_id = c.id AND m2.user_id = ?
       WHERE c.type = 'private'`,
      [req.userId, targetId]
    );

    let chatId;
    if (existing) {
      chatId = existing.id;
    } else {
      const now = Date.now();
      const result = await run("INSERT INTO chats (type, created_at) VALUES ('private', ?)", [now]);
      chatId = result.lastID;
      await run('INSERT INTO chat_members (chat_id, user_id) VALUES (?, ?)', [chatId, req.userId]);
      await run('INSERT INTO chat_members (chat_id, user_id) VALUES (?, ?)', [chatId, targetId]);
    }

    res.json({
      chat: {
        id: chatId,
        type: 'private',
        peer: publicUser(targetUser),
        last_message: null,
        unread_count: 0
      }
    });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to create chat' });
  }
});

app.get('/api/chats/:id/messages', requireAuth, async (req, res) => {
  try {
    const chatId = parseInt(req.params.id, 10);
    if (!(await isChatMember(chatId, req.userId))) {
      return res.status(403).json({ error: 'Access denied' });
    }
    const rows = await all(
      'SELECT * FROM messages WHERE chat_id = ? ORDER BY created_at ASC LIMIT 500',
      [chatId]
    );
    const messages = rows.map((m) => ({
      id: m.id,
      chat_id: m.chat_id,
      sender_id: m.sender_id,
      text: m.deleted ? '' : m.text,
      created_at: m.created_at,
      edited_at: m.edited_at,
      is_read: !!m.is_read,
      deleted: !!m.deleted
    }));
    res.json({ messages: messages });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to load messages' });
  }
});

app.post('/api/messages', requireAuth, async (req, res) => {
  try {
    const body = req.body || {};
    const chatId = parseInt(body.chatId, 10);
    const text = (body.text || '').toString().trim();

    if (!chatId) return res.status(400).json({ error: 'chatId is required' });
    if (!text) return res.status(400).json({ error: 'Message text is required' });
    if (text.length > MAX_MESSAGE_LENGTH) return res.status(400).json({ error: 'Message too long' });
    if (!(await isChatMember(chatId, req.userId))) {
      return res.status(403).json({ error: 'Access denied' });
    }

    const now = Date.now();
    const result = await run(
      'INSERT INTO messages (chat_id, sender_id, text, created_at, edited_at, is_read, deleted) VALUES (?, ?, ?, ?, NULL, 0, 0)',
      [chatId, req.userId, text, now]
    );

    const message = {
      id: result.lastID,
      chat_id: chatId,
      sender_id: req.userId,
      text: text,
      created_at: now,
      edited_at: null,
      is_read: false,
      deleted: false
    };

    broadcastNewMessage(chatId, message);
    res.json({ message: message });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to send message' });
  }
});

app.put('/api/messages/:id', requireAuth, async (req, res) => {
  try {
    const id = parseInt(req.params.id, 10);
    const text = ((req.body || {}).text || '').toString().trim();
    if (!text) return res.status(400).json({ error: 'Message text is required' });
    if (text.length > MAX_MESSAGE_LENGTH) return res.status(400).json({ error: 'Message too long' });

    const msg = await get('SELECT * FROM messages WHERE id = ?', [id]);
    if (!msg || msg.deleted) return res.status(404).json({ error: 'Message not found' });
    if (msg.sender_id !== req.userId) return res.status(403).json({ error: 'You can only edit your own messages' });

    const now = Date.now();
    await run('UPDATE messages SET text = ?, edited_at = ? WHERE id = ?', [text, now, id]);

    broadcastToChat(msg.chat_id, 'message_edited', {
      id: id,
      chat_id: msg.chat_id,
      text: text,
      edited_at: now
    });

    res.json({ ok: true });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to edit message' });
  }
});

app.delete('/api/messages/:id', requireAuth, async (req, res) => {
  try {
    const id = parseInt(req.params.id, 10);
    const msg = await get('SELECT * FROM messages WHERE id = ?', [id]);
    if (!msg || msg.deleted) return res.status(404).json({ error: 'Message not found' });
    if (msg.sender_id !== req.userId) return res.status(403).json({ error: 'You can only delete your own messages' });

    await run('UPDATE messages SET deleted = 1 WHERE id = ?', [id]);

    broadcastToChat(msg.chat_id, 'message_deleted', {
      id: id,
      chat_id: msg.chat_id
    });

    res.json({ ok: true });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to delete message' });
  }
});

function buildCookie(name, value, expire) {
  const parts = [name + '=' + encodeURIComponent(value), 'Path=/', 'HttpOnly', 'SameSite=Lax'];
  if (expire) {
    parts.push('Expires=Thu, 01 Jan 1970 00:00:00 GMT');
  } else {
    parts.push('Max-Age=' + Math.floor(SESSION_MAX_AGE_MS / 1000));
  }
  return parts.join('; ');
}

// ========================================
// SOCKET.IO
// ========================================

const server = http.createServer(app);
const io = new Server(server, { cors: { origin: true, credentials: true } });

// userId -> Set of socket ids currently connected
const onlineSockets = new Map();

function markOnline(userId, socketId) {
  if (!onlineSockets.has(userId)) onlineSockets.set(userId, new Set());
  onlineSockets.get(userId).add(socketId);
}

function markOffline(userId, socketId) {
  const set = onlineSockets.get(userId);
  if (!set) return false;
  set.delete(socketId);
  if (set.size === 0) {
    onlineSockets.delete(userId);
    return true; // fully offline now
  }
  return false;
}

function isOnline(userId) {
  return onlineSockets.has(userId);
}

async function broadcastNewMessage(chatId, message) {
  io.to('chat:' + chatId).emit('new_message', message);
  const members = await all('SELECT user_id FROM chat_members WHERE chat_id = ?', [chatId]);
  members.forEach((m) => {
    io.to('user:' + m.user_id).emit('chat_updated', { chat_id: chatId });
  });
}

function broadcastToChat(chatId, event, payload) {
  io.to('chat:' + chatId).emit(event, payload);
}

io.use((socket, next) => {
  const cookies = parseCookies(socket.handshake.headers.cookie);
  const token = cookies[SESSION_COOKIE];
  const session = token ? sessions.get(token) : null;
  if (!session) {
    return next(new Error('unauthorized'));
  }
  socket.userId = session.userId;
  next();
});

io.on('connection', async (socket) => {
  const userId = socket.userId;
  socket.join('user:' + userId);
  markOnline(userId, socket.id);

  await run('UPDATE users SET last_seen = ? WHERE id = ?', [Date.now(), userId]);
  socket.broadcast.emit('user_online', { user_id: userId });

  // Join rooms for every chat this user belongs to, so message events reach them
  const myChats = await all('SELECT chat_id FROM chat_members WHERE user_id = ?', [userId]);
  myChats.forEach((row) => socket.join('chat:' + row.chat_id));

  socket.on('join_chat', async (chatId) => {
    if (await isChatMember(chatId, userId)) {
      socket.join('chat:' + chatId);
    }
  });

  socket.on('typing', async (chatId) => {
    if (await isChatMember(chatId, userId)) {
      socket.to('chat:' + chatId).emit('typing', { chat_id: chatId, user_id: userId });
    }
  });

  socket.on('stop_typing', async (chatId) => {
    if (await isChatMember(chatId, userId)) {
      socket.to('chat:' + chatId).emit('stop_typing', { chat_id: chatId, user_id: userId });
    }
  });

  socket.on('message_read', async (chatId) => {
    try {
      if (!(await isChatMember(chatId, userId))) return;
      await run(
        'UPDATE messages SET is_read = 1 WHERE chat_id = ? AND sender_id != ? AND is_read = 0',
        [chatId, userId]
      );
      io.to('chat:' + chatId).emit('message_read', { chat_id: chatId, reader_id: userId });
    } catch (err) {
      console.error(err);
    }
  });

  socket.on('disconnect', async () => {
    const fullyOffline = markOffline(userId, socket.id);
    if (fullyOffline) {
      const now = Date.now();
      await run('UPDATE users SET last_seen = ? WHERE id = ?', [now, userId]);
      io.emit('user_offline', { user_id: userId, last_seen: now });
    }
  });
});

// ========================================
// HTML
// ========================================

function renderPage() {
  return `<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover">
<title>Monogram</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 32 32%22><rect width=%2232%22 height=%2232%22 rx=%228%22 fill=%22%234C7EFF%22/><text x=%2216%22 y=%2222%22 font-family=%22sans-serif%22 font-weight=%22700%22 font-size=%2216%22 fill=%22white%22 text-anchor=%22middle%22>M</text></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
${renderCSS()}
</style>
</head>
<body>
<div id="toast-root" class="toast-root"></div>

<div id="auth-view" class="auth-view">
  <div class="auth-card">
    <div class="auth-brand">
      <div class="brand-mark">M</div>
      <div class="brand-name">Monogram</div>
    </div>
    <div class="auth-tabs">
      <button class="auth-tab active" data-tab="login">Log in</button>
      <button class="auth-tab" data-tab="register">Sign up</button>
    </div>

    <form id="login-form" class="auth-form">
      <label class="field">
        <span>Username</span>
        <input type="text" id="login-username" autocomplete="username" required>
      </label>
      <label class="field">
        <span>Password</span>
        <input type="password" id="login-password" autocomplete="current-password" required>
      </label>
      <div class="auth-error" id="login-error"></div>
      <button type="submit" class="btn-primary">Log in</button>
    </form>

    <form id="register-form" class="auth-form hidden">
      <label class="field">
        <span>Username</span>
        <input type="text" id="reg-username" autocomplete="username" required>
      </label>
      <label class="field">
        <span>Display name</span>
        <input type="text" id="reg-displayname" autocomplete="name">
      </label>
      <label class="field">
        <span>Password</span>
        <input type="password" id="reg-password" autocomplete="new-password" required>
      </label>
      <div class="auth-error" id="register-error"></div>
      <button type="submit" class="btn-primary">Create account</button>
    </form>
  </div>
</div>

<div id="app-view" class="app-view hidden">
  <aside id="sidebar" class="sidebar">
    <div class="sidebar-header">
      <button id="profile-btn" class="avatar-btn" title="Your profile"><span id="my-avatar" class="avatar"></span></button>
      <div class="search-wrap">
        <input id="search-input" type="text" placeholder="Search @username">
      </div>
      <button id="theme-toggle" class="icon-btn" title="Toggle theme">
        <svg id="theme-icon" width="20" height="20" viewBox="0 0 24 24" fill="none"><path d="M12 3v1M12 20v1M4.2 4.2l.7.7M19.1 19.1l.7.7M3 12h1M20 12h1M4.2 19.8l.7-.7M19.1 4.9l.7-.7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><circle cx="12" cy="12" r="4.5" stroke="currentColor" stroke-width="1.6"/></svg>
      </button>
      <button id="settings-btn" class="icon-btn" title="Settings">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none"><path d="M12 8.5a3.5 3.5 0 100 7 3.5 3.5 0 000-7z" stroke="currentColor" stroke-width="1.6"/><path d="M19 12a7 7 0 00-.1-1.2l1.9-1.5-1.5-2.6-2.2.8a7 7 0 00-2-1.2L14.8 4h-3l-.3 2.3a7 7 0 00-2 1.2l-2.2-.8-1.5 2.6 1.9 1.5A7 7 0 005 12c0 .4 0 .8.1 1.2l-1.9 1.5 1.5 2.6 2.2-.8c.6.5 1.3.9 2 1.2L9.2 20h3l.3-2.3c.7-.3 1.4-.7 2-1.2l2.2.8 1.5-2.6-1.9-1.5c.1-.4.1-.8.1-1.2z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>
      </button>
    </div>
    <div id="search-results" class="search-results hidden"></div>
    <div id="chat-list" class="chat-list"></div>
  </aside>

  <main id="chat-panel" class="chat-panel">
    <div id="empty-state" class="empty-state">
      <div class="empty-mark">M</div>
      <p>Select a chat, or search for someone to message.</p>
    </div>
    <div id="chat-view" class="chat-view hidden">
      <div class="chat-header">
        <button id="back-btn" class="icon-btn back-btn" title="Back">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none"><path d="M15 19l-7-7 7-7" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </button>
        <span id="chat-avatar" class="avatar"></span>
        <div class="chat-header-info">
          <div id="chat-header-name" class="chat-header-name"></div>
          <div id="chat-header-status" class="chat-header-status"></div>
        </div>
      </div>
      <div id="messages" class="messages"></div>
      <div class="composer">
        <button id="emoji-btn" class="icon-btn" title="Emoji">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.6"/><circle cx="9" cy="10" r="1" fill="currentColor"/><circle cx="15" cy="10" r="1" fill="currentColor"/><path d="M8.5 14.5c1 1.2 2.2 1.8 3.5 1.8s2.5-.6 3.5-1.8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
        </button>
        <div id="emoji-picker" class="emoji-picker hidden"></div>
        <textarea id="msg-input" rows="1" placeholder="Write a message..."></textarea>
        <button id="send-btn" class="send-btn" title="Send">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none"><path d="M4 12l16-8-6 16-3-6-7-2z" fill="currentColor"/></svg>
        </button>
      </div>
    </div>
  </main>
</div>

<div id="profile-modal" class="modal-overlay hidden">
  <div class="modal">
    <div class="modal-title">Your profile</div>
    <label class="field">
      <span>Display name</span>
      <input type="text" id="profile-displayname">
    </label>
    <label class="field">
      <span>Avatar URL</span>
      <input type="text" id="profile-avatar" placeholder="https://...">
    </label>
    <div class="modal-actions">
      <button class="btn-secondary" id="profile-cancel">Cancel</button>
      <button class="btn-primary" id="profile-save">Save</button>
    </div>
  </div>
</div>

<div id="settings-modal" class="modal-overlay hidden">
  <div class="modal">
    <div class="modal-title">Settings</div>
    <div class="settings-row">
      <span>Sounds</span>
      <label class="switch"><input type="checkbox" id="sound-toggle" checked><span class="switch-track"></span></label>
    </div>
    <div class="settings-row">
      <span>Theme</span>
      <div class="theme-pick">
        <button class="theme-opt" data-theme="dark">Dark</button>
        <button class="theme-opt" data-theme="light">Light</button>
      </div>
    </div>
    <div class="settings-row">
      <button class="btn-secondary logout-btn" id="logout-btn">Log out</button>
    </div>
    <div class="modal-actions">
      <button class="btn-secondary" id="settings-close">Close</button>
    </div>
  </div>
</div>

<script src="/socket.io/socket.io.js"></script>
<script>
${renderClientJS()}
</script>
</body>
</html>`;
}

// ========================================
// CSS
// ========================================

function renderCSS() {
  return `
:root {
  --bg: #0F1115;
  --surface: #161922;
  --surface-2: #1D2130;
  --border: #262B3A;
  --text: #ECEDF1;
  --text-dim: #8B909C;
  --accent: #4C7EFF;
  --accent-2: #34D399;
  --danger: #FF5C6C;
  --bubble-out: #2A4BD6;
  --bubble-in: #1D2130;
  --radius: 14px;
  --font-display: 'Space Grotesk', 'Segoe UI', sans-serif;
  --font-body: 'Inter', 'Segoe UI', sans-serif;
}
html[data-theme="light"] {
  --bg: #F5F6FA;
  --surface: #FFFFFF;
  --surface-2: #F0F1F6;
  --border: #E3E5EE;
  --text: #14151A;
  --text-dim: #6B7080;
  --accent: #4C7EFF;
  --accent-2: #0FA36B;
  --danger: #E0374A;
  --bubble-out: #4C7EFF;
  --bubble-in: #F0F1F6;
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-body);
  -webkit-font-smoothing: antialiased;
  overflow: hidden;
}
.hidden { display: none !important; }
button { font-family: inherit; cursor: pointer; }
input, textarea { font-family: inherit; }

/* ---------- Auth ---------- */
.auth-view {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  animation: fadeIn .35s ease;
}
.auth-card {
  width: 100%;
  max-width: 380px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 20px;
  padding: 32px 28px;
}
.auth-brand { display: flex; align-items: center; gap: 12px; margin-bottom: 24px; }
.brand-mark {
  width: 42px; height: 42px; border-radius: 12px;
  background: linear-gradient(135deg, var(--accent), #7C5CFC);
  display: flex; align-items: center; justify-content: center;
  font-family: var(--font-display); font-weight: 700; color: white; font-size: 20px;
}
.brand-name { font-family: var(--font-display); font-weight: 700; font-size: 22px; letter-spacing: -.02em; }
.auth-tabs { display: flex; gap: 4px; background: var(--surface-2); padding: 4px; border-radius: 10px; margin-bottom: 20px; }
.auth-tab {
  flex: 1; padding: 9px 0; border: none; background: transparent; color: var(--text-dim);
  border-radius: 8px; font-weight: 600; font-size: 14px; transition: all .15s ease;
}
.auth-tab.active { background: var(--surface); color: var(--text); box-shadow: 0 1px 3px rgba(0,0,0,.15); }
.auth-form { display: flex; flex-direction: column; gap: 14px; animation: fadeIn .25s ease; }
.field { display: flex; flex-direction: column; gap: 6px; font-size: 13px; color: var(--text-dim); }
.field input {
  padding: 11px 13px; border-radius: 10px; border: 1px solid var(--border);
  background: var(--surface-2); color: var(--text); font-size: 14.5px; outline: none;
  transition: border-color .15s ease;
}
.field input:focus { border-color: var(--accent); }
.auth-error { color: var(--danger); font-size: 13px; min-height: 4px; }
.btn-primary {
  padding: 12px; border-radius: 10px; border: none; background: var(--accent); color: white;
  font-weight: 600; font-size: 14.5px; transition: filter .15s ease, transform .1s ease;
}
.btn-primary:hover { filter: brightness(1.08); }
.btn-primary:active { transform: scale(.98); }
.btn-secondary {
  padding: 10px 16px; border-radius: 10px; border: 1px solid var(--border); background: transparent;
  color: var(--text); font-weight: 600; font-size: 14px;
}

/* ---------- App layout ---------- */
.app-view { height: 100%; display: flex; }
.sidebar {
  width: 340px; min-width: 340px; background: var(--surface); border-right: 1px solid var(--border);
  display: flex; flex-direction: column;
}
.sidebar-header { display: flex; align-items: center; gap: 8px; padding: 14px; border-bottom: 1px solid var(--border); }
.search-wrap { flex: 1; }
#search-input {
  width: 100%; padding: 9px 12px; border-radius: 10px; border: 1px solid var(--border);
  background: var(--surface-2); color: var(--text); font-size: 14px; outline: none;
}
#search-input:focus { border-color: var(--accent); }
.icon-btn, .avatar-btn {
  width: 38px; height: 38px; border-radius: 10px; border: none; background: var(--surface-2);
  color: var(--text-dim); display: flex; align-items: center; justify-content: center; flex-shrink: 0;
  transition: background .15s ease, color .15s ease;
}
.icon-btn:hover, .avatar-btn:hover { background: var(--border); color: var(--text); }
.avatar-btn { padding: 0; overflow: hidden; }

.search-results, .chat-list { overflow-y: auto; flex: 1; }
.search-results.hidden, .chat-list.hidden { display: none; }

.avatar {
  width: 42px; height: 42px; border-radius: 50%; flex-shrink: 0; display: flex; align-items: center;
  justify-content: center; font-family: var(--font-display); font-weight: 600; font-size: 15px;
  color: white; background-size: cover; background-position: center;
}
.chat-row, .user-row {
  display: flex; align-items: center; gap: 12px; padding: 11px 14px; cursor: pointer;
  border-bottom: 1px solid transparent; transition: background .12s ease;
}
.chat-row:hover, .user-row:hover { background: var(--surface-2); }
.chat-row.active { background: var(--surface-2); }
.chat-row-body, .user-row-body { flex: 1; min-width: 0; }
.chat-row-top { display: flex; justify-content: space-between; align-items: baseline; gap: 6px; }
.chat-row-name, .user-row-name { font-weight: 600; font-size: 14.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.chat-row-time { font-size: 11.5px; color: var(--text-dim); flex-shrink: 0; }
.chat-row-bottom { display: flex; justify-content: space-between; align-items: center; gap: 6px; margin-top: 2px; }
.chat-row-preview { font-size: 13px; color: var(--text-dim); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.user-row-username { font-size: 12.5px; color: var(--text-dim); }
.unread-badge {
  background: var(--accent); color: white; font-size: 11px; font-weight: 700; min-width: 18px; height: 18px;
  border-radius: 9px; display: flex; align-items: center; justify-content: center; padding: 0 5px; flex-shrink: 0;
}
.empty-list-hint { padding: 24px 16px; color: var(--text-dim); font-size: 13.5px; text-align: center; }

.chat-panel { flex: 1; position: relative; display: flex; flex-direction: column; min-width: 0; }
.empty-state {
  flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center;
  color: var(--text-dim); gap: 14px;
}
.empty-mark {
  width: 64px; height: 64px; border-radius: 18px; background: var(--surface);
  border: 1px solid var(--border); display: flex; align-items: center; justify-content: center;
  font-family: var(--font-display); font-weight: 700; font-size: 28px; color: var(--text-dim);
}
.chat-view { flex: 1; display: flex; flex-direction: column; min-height: 0; animation: fadeIn .2s ease; }
.chat-header {
  display: flex; align-items: center; gap: 12px; padding: 12px 16px; border-bottom: 1px solid var(--border);
  background: var(--surface);
}
.back-btn { display: none; }
.chat-header-name { font-weight: 600; font-size: 15px; }
.chat-header-status { font-size: 12.5px; color: var(--text-dim); }
.chat-header-status.online { color: var(--accent-2); }

.messages { flex: 1; overflow-y: auto; padding: 18px 16px; display: flex; flex-direction: column; gap: 3px; }
.msg-row { display: flex; margin-top: 8px; animation: msgIn .18s ease; }
.msg-row.out { justify-content: flex-end; }
@keyframes msgIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
.bubble {
  max-width: 70%; padding: 9px 12px; border-radius: 16px; font-size: 14.5px; line-height: 1.4;
  position: relative; word-wrap: break-word; white-space: pre-wrap;
}
.msg-row.in .bubble { background: var(--bubble-in); border-bottom-left-radius: 4px; }
.msg-row.out .bubble { background: var(--bubble-out); color: white; border-bottom-right-radius: 4px; }
.bubble-meta { display: flex; gap: 5px; align-items: center; margin-top: 3px; font-size: 10.5px; opacity: .75; justify-content: flex-end; }
.bubble.deleted { font-style: italic; opacity: .6; }
.bubble-actions { display: none; gap: 4px; position: absolute; top: -26px; right: 0; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 3px; }
.msg-row.out:hover .bubble-actions { display: flex; }
.bubble-actions button { border: none; background: transparent; color: var(--text-dim); font-size: 11px; padding: 3px 6px; border-radius: 5px; }
.bubble-actions button:hover { background: var(--surface-2); color: var(--text); }
.typing-indicator { font-size: 12.5px; color: var(--text-dim); padding: 4px 4px; font-style: italic; min-height: 18px; }

.composer { display: flex; align-items: flex-end; gap: 8px; padding: 12px 14px; border-top: 1px solid var(--border); background: var(--surface); position: relative; }
#msg-input {
  flex: 1; resize: none; max-height: 120px; padding: 10px 14px; border-radius: 18px; border: 1px solid var(--border);
  background: var(--surface-2); color: var(--text); font-size: 14.5px; outline: none; line-height: 1.4;
}
#msg-input:focus { border-color: var(--accent); }
.send-btn {
  width: 40px; height: 40px; border-radius: 50%; border: none; background: var(--accent); color: white;
  display: flex; align-items: center; justify-content: center; flex-shrink: 0; transition: filter .15s ease, transform .1s ease;
}
.send-btn:hover { filter: brightness(1.1); }
.send-btn:active { transform: scale(.94); }
.emoji-picker {
  position: absolute; bottom: 58px; left: 12px; width: 260px; max-height: 200px; overflow-y: auto;
  background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 8px;
  display: grid; grid-template-columns: repeat(7, 1fr); gap: 2px; animation: fadeIn .12s ease;
  box-shadow: 0 8px 24px rgba(0,0,0,.25);
}
.emoji-picker button { border: none; background: transparent; font-size: 19px; padding: 5px; border-radius: 6px; }
.emoji-picker button:hover { background: var(--surface-2); }

/* ---------- Modals ---------- */
.modal-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,.5); display: flex; align-items: center; justify-content: center;
  z-index: 50; animation: fadeIn .15s ease; padding: 16px;
}
.modal {
  background: var(--surface); border: 1px solid var(--border); border-radius: 16px; padding: 22px;
  width: 100%; max-width: 360px; animation: modalIn .18s ease;
}
@keyframes modalIn { from { opacity: 0; transform: scale(.96); } to { opacity: 1; transform: scale(1); } }
.modal-title { font-family: var(--font-display); font-weight: 700; font-size: 17px; margin-bottom: 16px; }
.modal .field { margin-bottom: 12px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 18px; }
.settings-row { display: flex; align-items: center; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid var(--border); font-size: 14px; }
.settings-row:last-of-type { border-bottom: none; }
.logout-btn { color: var(--danger); border-color: var(--danger); width: 100%; }
.theme-pick { display: flex; gap: 6px; }
.theme-opt { padding: 6px 12px; border-radius: 8px; border: 1px solid var(--border); background: var(--surface-2); color: var(--text-dim); font-size: 12.5px; font-weight: 600; }
.theme-opt.active { background: var(--accent); color: white; border-color: var(--accent); }
.switch { position: relative; display: inline-block; width: 38px; height: 22px; }
.switch input { opacity: 0; width: 0; height: 0; }
.switch-track { position: absolute; inset: 0; background: var(--border); border-radius: 22px; transition: background .15s ease; }
.switch-track::before { content: ""; position: absolute; width: 16px; height: 16px; left: 3px; top: 3px; background: white; border-radius: 50%; transition: transform .15s ease; }
.switch input:checked + .switch-track { background: var(--accent); }
.switch input:checked + .switch-track::before { transform: translateX(16px); }

/* ---------- Toast ---------- */
.toast-root { position: fixed; top: 16px; left: 50%; transform: translateX(-50%); z-index: 100; display: flex; flex-direction: column; gap: 8px; }
.toast {
  background: var(--surface); border: 1px solid var(--border); color: var(--text); padding: 10px 16px;
  border-radius: 10px; font-size: 13.5px; box-shadow: 0 6px 20px rgba(0,0,0,.2); animation: toastIn .2s ease;
}
@keyframes toastIn { from { opacity: 0; transform: translateY(-8px); } to { opacity: 1; transform: translateY(0); } }

@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }

::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 8px; }

/* ---------- Mobile ---------- */
@media (max-width: 820px) {
  .sidebar { width: 100%; min-width: 0; }
  .chat-panel { position: absolute; inset: 0; z-index: 5; background: var(--bg); transform: translateX(100%); transition: transform .22s ease; }
  .chat-panel.mobile-open { transform: translateX(0); }
  .back-btn { display: flex; }
  .app-view { position: relative; overflow: hidden; }
  .bubble { max-width: 82%; }
}
`;
}

// ========================================
// FRONTEND JAVASCRIPT
// ========================================

function renderClientJS() {
  return `
(function () {
  'use strict';

  var state = {
    user: null,
    chats: [],
    chatsById: {},
    currentChatId: null,
    messagesCache: {},
    socket: null,
    typingTimer: null,
    remoteTypingTimer: null,
    soundOn: true,
    theme: 'dark'
  };

  var EMOJI = ['😀','😂','😍','😊','😉','😎','🤔','😢','😭','😡','👍','👎','🙏','👏','🔥','🎉','❤️','💯','✨','😴','🥳','😱','🤝','👌','🙌','🤷','😅','🙂','😇','🤩'];

  // ---------- utils ----------

  function qs(id) { return document.getElementById(id); }

  function el(tag, className) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    return node;
  }

  function api(method, url, body) {
    var opts = {
      method: method,
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin'
    };
    if (body !== undefined) opts.body = JSON.stringify(body);
    return fetch(url, opts).then(function (res) {
      return res.json().then(function (data) {
        if (!res.ok) {
          var err = new Error(data && data.error ? data.error : 'Request failed');
          throw err;
        }
        return data;
      });
    });
  }

  function toast(message) {
    var root = qs('toast-root');
    var t = el('div', 'toast');
    t.textContent = message;
    root.appendChild(t);
    setTimeout(function () {
      t.remove();
    }, 3200);
  }

  function initialsFor(name) {
    var parts = (name || '?').trim().split(/\\s+/);
    var s = parts[0] ? parts[0].charAt(0) : '?';
    if (parts.length > 1 && parts[1]) s += parts[1].charAt(0);
    return s.toUpperCase();
  }

  function hueFor(str) {
    var hash = 0;
    for (var i = 0; i < str.length; i++) {
      hash = (hash << 5) - hash + str.charCodeAt(i);
      hash |= 0;
    }
    return Math.abs(hash) % 360;
  }

  function paintAvatar(node, user) {
    node.textContent = '';
    node.style.backgroundImage = '';
    if (user && user.avatar) {
      node.style.backgroundImage = 'url(\\'' + user.avatar.replace(/'/g, '') + '\\')';
      node.style.background = node.style.backgroundImage + ' center/cover no-repeat';
    } else {
      var name = user ? (user.display_name || user.username || '?') : '?';
      var hue = hueFor(user ? (user.username || name) : 'x');
      node.style.background = 'linear-gradient(135deg, hsl(' + hue + ',70%,55%), hsl(' + ((hue + 40) % 360) + ',70%,45%))';
      node.textContent = initialsFor(name);
    }
  }

  function timeLabel(ts) {
    var d = new Date(ts);
    var now = new Date();
    var sameDay = d.toDateString() === now.toDateString();
    var h = d.getHours();
    var m = d.getMinutes();
    var hh = (h < 10 ? '0' : '') + h;
    var mm = (m < 10 ? '0' : '') + m;
    if (sameDay) return hh + ':' + mm;
    return (d.getMonth() + 1) + '/' + d.getDate();
  }

  function lastSeenLabel(ts) {
    if (!ts) return 'offline';
    var diff = Date.now() - ts;
    if (diff < 2 * 60 * 1000) return 'last seen just now';
    if (diff < 60 * 60 * 1000) return 'last seen recently';
    if (diff < 24 * 60 * 60 * 1000) return 'last seen today';
    return 'last seen recently';
  }

  // ---------- sound (Web Audio API, no audio files) ----------

  var audioCtx = null;
  function beep(freq, dur, gain) {
    if (!state.soundOn) return;
    try {
      if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      var osc = audioCtx.createOscillator();
      var g = audioCtx.createGain();
      osc.type = 'sine';
      osc.frequency.value = freq;
      g.gain.value = gain || 0.05;
      osc.connect(g);
      g.connect(audioCtx.destination);
      osc.start();
      g.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + dur);
      osc.stop(audioCtx.currentTime + dur);
    } catch (e) { /* ignore */ }
  }
  function soundSend() { beep(720, 0.08, 0.04); }
  function soundReceive() { beep(500, 0.12, 0.05); }
  function soundNotify() { beep(880, 0.15, 0.05); }

  // ---------- theme ----------

  function applyTheme(theme) {
    state.theme = theme;
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('mg_theme', theme);
    document.querySelectorAll('.theme-opt').forEach(function (btn) {
      btn.classList.toggle('active', btn.getAttribute('data-theme') === theme);
    });
  }

  // ---------- auth views ----------

  function showAuthView() {
    qs('auth-view').classList.remove('hidden');
    qs('app-view').classList.add('hidden');
  }

  function showAppView() {
    qs('auth-view').classList.add('hidden');
    qs('app-view').classList.remove('hidden');
    paintAvatar(qs('my-avatar'), state.user);
  }

  qs('login-form').addEventListener('submit', function (e) {
    e.preventDefault();
    qs('login-error').textContent = '';
    api('POST', '/api/login', {
      username: qs('login-username').value.trim(),
      password: qs('login-password').value
    }).then(function (data) {
      state.user = data.user;
      afterAuth();
    }).catch(function (err) {
      qs('login-error').textContent = err.message;
    });
  });

  qs('register-form').addEventListener('submit', function (e) {
    e.preventDefault();
    qs('register-error').textContent = '';
    api('POST', '/api/register', {
      username: qs('reg-username').value.trim(),
      display_name: qs('reg-displayname').value.trim(),
      password: qs('reg-password').value
    }).then(function (data) {
      state.user = data.user;
      afterAuth();
    }).catch(function (err) {
      qs('register-error').textContent = err.message;
    });
  });

  document.querySelectorAll('.auth-tab').forEach(function (tab) {
    tab.addEventListener('click', function () {
      document.querySelectorAll('.auth-tab').forEach(function (t) { t.classList.remove('active'); });
      tab.classList.add('active');
      var which = tab.getAttribute('data-tab');
      qs('login-form').classList.toggle('hidden', which !== 'login');
      qs('register-form').classList.toggle('hidden', which !== 'register');
    });
  });

  // ---------- bootstrap after auth ----------

  function afterAuth() {
    showAppView();
    connectSocket();
    loadChats();
  }

  function connectSocket() {
    if (state.socket) state.socket.disconnect();
    state.socket = io({ withCredentials: true });

    state.socket.on('new_message', function (msg) {
      cacheMessage(msg);
      if (msg.chat_id === state.currentChatId) {
        renderMessages(state.currentChatId);
        if (msg.sender_id !== state.user.id) {
          state.socket.emit('message_read', state.currentChatId);
          soundReceive();
        }
      } else if (msg.sender_id !== state.user.id) {
        soundNotify();
      }
      loadChats();
    });

    state.socket.on('chat_updated', function () { loadChats(); });

    state.socket.on('typing', function (data) {
      if (data.chat_id === state.currentChatId && data.user_id !== state.user.id) {
        showRemoteTyping(true);
      }
    });
    state.socket.on('stop_typing', function (data) {
      if (data.chat_id === state.currentChatId && data.user_id !== state.user.id) {
        showRemoteTyping(false);
      }
    });

    state.socket.on('message_read', function (data) {
      if (data.chat_id === state.currentChatId) {
        var list = state.messagesCache[data.chat_id] || [];
        list.forEach(function (m) { if (m.sender_id === state.user.id) m.is_read = true; });
        renderMessages(state.currentChatId);
      }
    });

    state.socket.on('message_edited', function (data) {
      var list = state.messagesCache[data.chat_id] || [];
      list.forEach(function (m) { if (m.id === data.id) { m.text = data.text; m.edited_at = data.edited_at; } });
      if (data.chat_id === state.currentChatId) renderMessages(data.chat_id);
      loadChats();
    });

    state.socket.on('message_deleted', function (data) {
      var list = state.messagesCache[data.chat_id] || [];
      list.forEach(function (m) { if (m.id === data.id) { m.deleted = true; m.text = ''; } });
      if (data.chat_id === state.currentChatId) renderMessages(data.chat_id);
      loadChats();
    });

    state.socket.on('user_online', function (data) { updatePeerStatus(data.user_id, true, null); });
    state.socket.on('user_offline', function (data) { updatePeerStatus(data.user_id, false, data.last_seen); });
  }

  function updatePeerStatus(userId, online, lastSeen) {
    var chat = null;
    for (var i = 0; i < state.chats.length; i++) {
      if (state.chats[i].peer && state.chats[i].peer.id === userId) { chat = state.chats[i]; break; }
    }
    if (chat && chat.peer) {
      if (lastSeen) chat.peer.last_seen = lastSeen;
      chat._online = online;
    }
    if (state.currentChatId && chat && chat.id === state.currentChatId) {
      setChatHeaderStatus(online, chat.peer ? chat.peer.last_seen : null);
    }
  }

  function cacheMessage(msg) {
    if (!state.messagesCache[msg.chat_id]) state.messagesCache[msg.chat_id] = [];
    var list = state.messagesCache[msg.chat_id];
    for (var i = 0; i < list.length; i++) {
      if (list[i].id === msg.id) { list[i] = msg; return; }
    }
    list.push(msg);
  }

  // ---------- chat list ----------

  function loadChats() {
    api('GET', '/api/chats').then(function (data) {
      state.chats = data.chats;
      state.chatsById = {};
      data.chats.forEach(function (c) { state.chatsById[c.id] = c; });
      renderChatList();
    }).catch(function () {});
  }

  function renderChatList() {
    var container = qs('chat-list');
    container.textContent = '';
    if (state.chats.length === 0) {
      var hint = el('div', 'empty-list-hint');
      hint.textContent = 'No chats yet. Search for a username above to start one.';
      container.appendChild(hint);
      return;
    }
    state.chats.forEach(function (chat) {
      var row = el('div', 'chat-row' + (chat.id === state.currentChatId ? ' active' : ''));
      var avatar = el('span', 'avatar');
      paintAvatar(avatar, chat.peer);
      var body = el('div', 'chat-row-body');
      var top = el('div', 'chat-row-top');
      var name = el('div', 'chat-row-name');
      name.textContent = chat.peer ? chat.peer.display_name : 'Unknown';
      top.appendChild(name);
      if (chat.last_message) {
        var time = el('span', 'chat-row-time');
        time.textContent = timeLabel(chat.last_message.created_at);
        top.appendChild(time);
      }
      var bottom = el('div', 'chat-row-bottom');
      var preview = el('div', 'chat-row-preview');
      if (chat.last_message) {
        var prefix = chat.last_message.sender_id === state.user.id ? 'You: ' : '';
        preview.textContent = prefix + (chat.last_message.deleted ? 'Message deleted' : chat.last_message.text);
      } else {
        preview.textContent = 'Say hello 👋';
      }
      bottom.appendChild(preview);
      if (chat.unread_count > 0) {
        var badge = el('span', 'unread-badge');
        badge.textContent = chat.unread_count > 99 ? '99+' : String(chat.unread_count);
        bottom.appendChild(badge);
      }
      body.appendChild(top);
      body.appendChild(bottom);
      row.appendChild(avatar);
      row.appendChild(body);
      row.addEventListener('click', function () { openChat(chat.id); });
      container.appendChild(row);
    });
  }

  // ---------- search ----------

  var searchDebounce = null;
  qs('search-input').addEventListener('input', function () {
    var q = this.value.trim();
    clearTimeout(searchDebounce);
    if (!q) {
      qs('search-results').classList.add('hidden');
      qs('chat-list').classList.remove('hidden');
      return;
    }
    searchDebounce = setTimeout(function () {
      api('GET', '/api/users/search?q=' + encodeURIComponent(q)).then(function (data) {
        renderSearchResults(data.users);
      }).catch(function () {});
    }, 220);
  });

  function renderSearchResults(users) {
    var container = qs('search-results');
    container.textContent = '';
    qs('chat-list').classList.add('hidden');
    container.classList.remove('hidden');
    if (users.length === 0) {
      var hint = el('div', 'empty-list-hint');
      hint.textContent = 'No users found.';
      container.appendChild(hint);
      return;
    }
    users.forEach(function (u) {
      var row = el('div', 'user-row');
      var avatar = el('span', 'avatar');
      paintAvatar(avatar, u);
      var body = el('div', 'user-row-body');
      var name = el('div', 'user-row-name');
      name.textContent = u.display_name;
      var uname = el('div', 'user-row-username');
      uname.textContent = '@' + u.username;
      body.appendChild(name);
      body.appendChild(uname);
      row.appendChild(avatar);
      row.appendChild(body);
      row.addEventListener('click', function () {
        api('POST', '/api/chats', { userId: u.id }).then(function (data) {
          qs('search-input').value = '';
          qs('search-results').classList.add('hidden');
          qs('chat-list').classList.remove('hidden');
          loadChats();
          openChat(data.chat.id);
        }).catch(function (err) { toast(err.message); });
      });
      container.appendChild(row);
    });
  }

  // ---------- chat view ----------

  function openChat(chatId) {
    state.currentChatId = chatId;
    qs('empty-state').classList.add('hidden');
    qs('chat-view').classList.remove('hidden');
    qs('chat-panel').classList.add('mobile-open');
    renderChatList();

    var chat = state.chatsById[chatId];
    if (chat) {
      paintAvatar(qs('chat-avatar'), chat.peer);
      qs('chat-header-name').textContent = chat.peer ? chat.peer.display_name : 'Unknown';
      setChatHeaderStatus(!!chat._online, chat.peer ? chat.peer.last_seen : null);
    }

    if (state.socket) {
      state.socket.emit('join_chat', chatId);
    }

    api('GET', '/api/chats/' + chatId + '/messages').then(function (data) {
      state.messagesCache[chatId] = data.messages;
      renderMessages(chatId);
      if (state.socket) state.socket.emit('message_read', chatId);
    }).catch(function (err) { toast(err.message); });
  }

  function setChatHeaderStatus(online, lastSeen) {
    var node = qs('chat-header-status');
    if (online) {
      node.textContent = 'Online';
      node.classList.add('online');
    } else {
      node.textContent = lastSeenLabel(lastSeen);
      node.classList.remove('online');
    }
  }

  qs('back-btn').addEventListener('click', function () {
    qs('chat-panel').classList.remove('mobile-open');
  });

  function renderMessages(chatId) {
    var container = qs('messages');
    container.textContent = '';
    var list = state.messagesCache[chatId] || [];
    var lastMine = null;
    list.forEach(function (m) {
      var row = el('div', 'msg-row ' + (m.sender_id === state.user.id ? 'out' : 'in'));
      var bubble = el('div', 'bubble' + (m.deleted ? ' deleted' : ''));
      bubble.textContent = m.deleted ? 'Message deleted' : m.text;
      var meta = el('div', 'bubble-meta');
      var metaText = timeLabel(m.created_at) + (m.edited_at ? ' · edited' : '');
      if (m.sender_id === state.user.id && !m.deleted) {
        metaText += m.is_read ? ' · ✓✓' : ' · ✓';
      }
      meta.textContent = metaText;
      bubble.appendChild(meta);

      if (m.sender_id === state.user.id && !m.deleted) {
        var actions = el('div', 'bubble-actions');
        var editBtn = el('button');
        editBtn.textContent = 'Edit';
        editBtn.addEventListener('click', function () { startEdit(m); });
        var delBtn = el('button');
        delBtn.textContent = 'Delete';
        delBtn.addEventListener('click', function () { deleteMessage(m); });
        actions.appendChild(editBtn);
        actions.appendChild(delBtn);
        row.appendChild(actions);
      }

      row.appendChild(bubble);
      container.appendChild(row);
    });
    container.scrollTop = container.scrollHeight;
  }

  var editingId = null;
  function startEdit(m) {
    editingId = m.id;
    var input = qs('msg-input');
    input.value = m.text;
    input.focus();
    toast('Editing message — press Enter to save');
  }

  function deleteMessage(m) {
    if (!confirm('Delete this message?')) return;
    api('DELETE', '/api/messages/' + m.id).then(function () {
      m.deleted = true;
      m.text = '';
      renderMessages(state.currentChatId);
      loadChats();
    }).catch(function (err) { toast(err.message); });
  }

  function showRemoteTyping(on) {
    var existing = document.getElementById('typing-indicator-row');
    if (on) {
      if (existing) return;
      var row = el('div', 'typing-indicator');
      row.id = 'typing-indicator-row';
      row.textContent = 'typing...';
      qs('messages').appendChild(row);
      qs('messages').scrollTop = qs('messages').scrollHeight;
    } else if (existing) {
      existing.remove();
    }
  }

  // ---------- composer ----------

  var msgInput = qs('msg-input');

  msgInput.addEventListener('input', function () {
    this.style.height = 'auto';
    this.style.height = Math.min(this.scrollHeight, 120) + 'px';
    if (!state.currentChatId || !state.socket) return;
    state.socket.emit('typing', state.currentChatId);
    clearTimeout(state.typingTimer);
    state.typingTimer = setTimeout(function () {
      state.socket.emit('stop_typing', state.currentChatId);
    }, 1200);
  });

  msgInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendCurrentMessage();
    }
  });

  qs('send-btn').addEventListener('click', sendCurrentMessage);

  function sendCurrentMessage() {
    var text = msgInput.value.trim();
    if (!text || !state.currentChatId) return;

    if (editingId) {
      var id = editingId;
      editingId = null;
      api('PUT', '/api/messages/' + id, { text: text }).then(function () {
        var list = state.messagesCache[state.currentChatId] || [];
        list.forEach(function (m) { if (m.id === id) { m.text = text; m.edited_at = Date.now(); } });
        renderMessages(state.currentChatId);
        loadChats();
      }).catch(function (err) { toast(err.message); });
      msgInput.value = '';
      msgInput.style.height = 'auto';
      return;
    }

    msgInput.value = '';
    msgInput.style.height = 'auto';
    if (state.socket) state.socket.emit('stop_typing', state.currentChatId);

    api('POST', '/api/messages', { chatId: state.currentChatId, text: text }).then(function (data) {
      cacheMessage(data.message);
      renderMessages(state.currentChatId);
      soundSend();
      loadChats();
    }).catch(function (err) { toast(err.message); });
  }

  // ---------- emoji ----------

  var emojiPicker = qs('emoji-picker');
  EMOJI.forEach(function (e) {
    var btn = el('button');
    btn.textContent = e;
    btn.addEventListener('click', function () {
      msgInput.value += e;
      msgInput.focus();
    });
    emojiPicker.appendChild(btn);
  });
  qs('emoji-btn').addEventListener('click', function (ev) {
    ev.stopPropagation();
    emojiPicker.classList.toggle('hidden');
  });
  document.addEventListener('click', function (e) {
    if (!emojiPicker.contains(e.target) && e.target.id !== 'emoji-btn') {
      emojiPicker.classList.add('hidden');
    }
  });

  // ---------- profile modal ----------

  qs('profile-btn').addEventListener('click', function () {
    qs('profile-displayname').value = state.user.display_name || '';
    qs('profile-avatar').value = state.user.avatar || '';
    qs('profile-modal').classList.remove('hidden');
  });
  qs('profile-cancel').addEventListener('click', function () {
    qs('profile-modal').classList.add('hidden');
  });
  qs('profile-save').addEventListener('click', function () {
    api('PUT', '/api/profile', {
      display_name: qs('profile-displayname').value.trim(),
      avatar: qs('profile-avatar').value.trim()
    }).then(function (data) {
      state.user = data.user;
      paintAvatar(qs('my-avatar'), state.user);
      qs('profile-modal').classList.add('hidden');
      toast('Profile updated');
      loadChats();
    }).catch(function (err) { toast(err.message); });
  });

  // ---------- settings modal ----------

  qs('settings-btn').addEventListener('click', function () {
    qs('settings-modal').classList.remove('hidden');
  });
  qs('settings-close').addEventListener('click', function () {
    qs('settings-modal').classList.add('hidden');
  });
  qs('sound-toggle').addEventListener('change', function () {
    state.soundOn = this.checked;
    localStorage.setItem('mg_sound', this.checked ? '1' : '0');
  });
  document.querySelectorAll('.theme-opt').forEach(function (btn) {
    btn.addEventListener('click', function () { applyTheme(btn.getAttribute('data-theme')); });
  });
  qs('theme-toggle').addEventListener('click', function () {
    applyTheme(state.theme === 'dark' ? 'light' : 'dark');
  });
  qs('logout-btn').addEventListener('click', function () {
    api('POST', '/api/logout').then(function () {
      if (state.socket) state.socket.disconnect();
      state.user = null;
      state.chats = [];
      state.currentChatId = null;
      qs('settings-modal').classList.add('hidden');
      showAuthView();
    });
  });

  // ---------- init ----------

  function init() {
    var savedTheme = localStorage.getItem('mg_theme') || 'dark';
    applyTheme(savedTheme);
    var savedSound = localStorage.getItem('mg_sound');
    state.soundOn = savedSound === null ? true : savedSound === '1';
    qs('sound-toggle').checked = state.soundOn;

    api('GET', '/api/me').then(function (data) {
      state.user = data.user;
      afterAuth();
    }).catch(function () {
      showAuthView();
    });
  }

  init();
})();
`;
}

app.get('/', (req, res) => {
  res.send(renderPage());
});

// ========================================
// SERVER START
// ========================================

initDatabase();

server.listen(PORT, () => {
  console.log('========================================');
  console.log('  Monogram is running');
  console.log('  http://localhost:' + PORT);
  console.log('  Database: ' + DB_PATH);
  console.log('========================================');
});
