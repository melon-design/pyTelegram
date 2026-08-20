// ========================================
// MONOGRAM
// A modern, real-time messenger — inspired by Telegram, but independent.
// One file. One command: node monogram.js
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

const PORT = process.env.PORT || 40000;
const HOST = '0.0.0.0';
const DB_PATH = path.join(__dirname, 'monogram.db');
const SESSION_COOKIE = 'mg_session';
const SESSION_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000; // 30 days
const MAX_MESSAGE_LENGTH = 4000;
const MIN_USERNAME_LENGTH = 3;
const MAX_USERNAME_LENGTH = 24;
const MIN_PASSWORD_LENGTH = 6;

// ========================================
// DATABASE HELPERS
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
        bio TEXT,
        created_at INTEGER NOT NULL,
        last_seen INTEGER NOT NULL
      )
    `);

    db.run(`
      CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
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
        PRIMARY KEY (chat_id, user_id),
        FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
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
        deleted INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE,
        FOREIGN KEY (sender_id) REFERENCES users(id) ON DELETE CASCADE
      )
    `);

    db.run(`
      CREATE TABLE IF NOT EXISTS contacts (
        user_id INTEGER NOT NULL,
        contact_id INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        PRIMARY KEY (user_id, contact_id),
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY (contact_id) REFERENCES users(id) ON DELETE CASCADE
      )
    `);

    db.run('CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id)');
    db.run('CREATE INDEX IF NOT EXISTS idx_chat_members_user ON chat_members(user_id)');
    db.run('CREATE INDEX IF NOT EXISTS idx_contacts_user ON contacts(user_id)');

    console.log('[Monogram] Database ready at ' + DB_PATH);
  });
}

// ========================================
// AUTHENTICATION HELPERS
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

async function createSession(userId) {
  const token = crypto.randomBytes(32).toString('hex');
  const now = Date.now();
  await run('INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)', [token, userId, now]);
  return token;
}

async function destroySession(token) {
  await run('DELETE FROM sessions WHERE token = ?', [token]);
}

async function getUserIdBySession(token) {
  if (!token) return null;
  const row = await get('SELECT user_id FROM sessions WHERE token = ?', [token]);
  return row ? row.user_id : null;
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

async function getUserIdFromRequest(req) {
  const cookies = parseCookies(req.headers.cookie);
  const token = cookies[SESSION_COOKIE];
  if (!token) return null;
  return await getUserIdBySession(token);
}

function requireAuth(req, res, next) {
  getUserIdFromRequest(req).then(userId => {
    if (!userId) {
      return res.status(401).json({ error: 'Not authenticated' });
    }
    req.userId = userId;
    next();
  }).catch(next);
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

function isValidBio(bio) {
  return typeof bio === 'string' && bio.length <= 200;
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
    bio: row.bio || null,
    last_seen: row.last_seen,
    created_at: row.created_at
  };
}

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
// EXPRESS APP
// ========================================

const app = express();
app.use(express.json({ limit: '1mb' }));

// ========================================
// STATIC FILES FOR PWA (icons, manifest, sw)
// ========================================

// Generate SVG icon
function generateIconSVG(size) {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
    <rect width="${size}" height="${size}" rx="${size * 0.2}" fill="#e53935"/>
    <text x="${size * 0.5}" y="${size * 0.7}" font-family="sans-serif" font-weight="700" font-size="${size * 0.6}" fill="white" text-anchor="middle">M</text>
  </svg>`;
}

app.get('/icon-192.svg', (req, res) => {
  res.type('image/svg+xml');
  res.send(generateIconSVG(192));
});

app.get('/icon-512.svg', (req, res) => {
  res.type('image/svg+xml');
  res.send(generateIconSVG(512));
});

app.get('/manifest.webmanifest', (req, res) => {
  const manifest = {
    name: 'Monogram',
    short_name: 'Monogram',
    description: 'A modern, real-time messenger.',
    start_url: '/',
    display: 'standalone',
    background_color: '#0a0a0a',
    theme_color: '#e53935',
    icons: [
      {
        src: '/icon-192.svg',
        sizes: '192x192',
        type: 'image/svg+xml',
        purpose: 'any maskable'
      },
      {
        src: '/icon-512.svg',
        sizes: '512x512',
        type: 'image/svg+xml',
        purpose: 'any maskable'
      }
    ]
  };
  res.json(manifest);
});

// Service Worker
app.get('/sw.js', (req, res) => {
  res.type('application/javascript');
  res.send(`
    // Service Worker for Monogram
    const CACHE_NAME = 'monogram-v1';
    const ASSETS = [
      '/',
      '/manifest.webmanifest',
      '/icon-192.svg',
      '/icon-512.svg'
    ];

    self.addEventListener('install', event => {
      event.waitUntil(
        caches.open(CACHE_NAME)
          .then(cache => cache.addAll(ASSETS))
          .then(() => self.skipWaiting())
      );
    });

    self.addEventListener('activate', event => {
      event.waitUntil(
        caches.keys().then(keys => {
          return Promise.all(keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key)));
        }).then(() => self.clients.claim())
      );
    });

    self.addEventListener('fetch', event => {
      // Skip API, Socket.IO, and dynamic content
      const url = new URL(event.request.url);
      if (url.pathname.startsWith('/api') || url.pathname.startsWith('/socket.io')) {
        return;
      }

      event.respondWith(
        caches.match(event.request)
          .then(cached => cached || fetch(event.request))
      );
    });
  `);
});

// ========================================
// API ROUTES
// ========================================

// Registration
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
      'INSERT INTO users (username, display_name, password_hash, avatar, bio, created_at, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?)',
      [username, displayName, passwordHash, null, null, now, now]
    );

    const token = await createSession(result.lastID);
    res.setHeader('Set-Cookie', buildCookie(SESSION_COOKIE, token));
    const user = await get('SELECT * FROM users WHERE id = ?', [result.lastID]);
    res.json({ user: publicUser(user) });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Registration failed' });
  }
});

// Login
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
    const token = await createSession(user.id);
    res.setHeader('Set-Cookie', buildCookie(SESSION_COOKIE, token));
    res.json({ user: publicUser(user) });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Login failed' });
  }
});

// Logout
app.post('/api/logout', requireAuth, async (req, res) => {
  const cookies = parseCookies(req.headers.cookie);
  const token = cookies[SESSION_COOKIE];
  if (token) await destroySession(token);
  res.setHeader('Set-Cookie', buildCookie(SESSION_COOKIE, '', true));
  res.json({ ok: true });
});

// Get current user
app.get('/api/me', requireAuth, async (req, res) => {
  const user = await get('SELECT * FROM users WHERE id = ?', [req.userId]);
  if (!user) return res.status(401).json({ error: 'Not authenticated' });
  res.json({ user: publicUser(user) });
});

// Update profile
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
    if (body.username !== undefined) {
      const newUsername = body.username.trim();
      if (!isValidUsername(newUsername)) {
        return res.status(400).json({ error: 'Invalid username' });
      }
      const existing = await get('SELECT id FROM users WHERE username = ? AND id != ? COLLATE NOCASE', [newUsername, req.userId]);
      if (existing) {
        return res.status(409).json({ error: 'Username already taken' });
      }
      updates.push('username = ?');
      params.push(newUsername);
    }
    if (body.avatar !== undefined) {
      const avatar = (body.avatar || '').trim();
      if (avatar.length > 500) return res.status(400).json({ error: 'Avatar URL too long' });
      updates.push('avatar = ?');
      params.push(avatar || null);
    }
    if (body.bio !== undefined) {
      const bio = (body.bio || '').trim();
      if (bio.length > 200) return res.status(400).json({ error: 'Bio too long' });
      updates.push('bio = ?');
      params.push(bio || null);
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

// Search users
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

// Contacts
app.get('/api/contacts', requireAuth, async (req, res) => {
  try {
    const rows = await all(
      `SELECT u.* FROM users u
       JOIN contacts c ON c.contact_id = u.id
       WHERE c.user_id = ?
       ORDER BY u.display_name COLLATE NOCASE`,
      [req.userId]
    );
    res.json({ contacts: rows.map(publicUser) });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to load contacts' });
  }
});

app.post('/api/contacts', requireAuth, async (req, res) => {
  try {
    const contactId = parseInt((req.body || {}).contactId, 10);
    if (!contactId || contactId === req.userId) {
      return res.status(400).json({ error: 'Invalid contact' });
    }
    const target = await get('SELECT id FROM users WHERE id = ?', [contactId]);
    if (!target) return res.status(404).json({ error: 'User not found' });

    const existing = await get('SELECT 1 FROM contacts WHERE user_id = ? AND contact_id = ?', [req.userId, contactId]);
    if (existing) return res.status(409).json({ error: 'Already in contacts' });

    await run('INSERT INTO contacts (user_id, contact_id, created_at) VALUES (?, ?, ?)', [req.userId, contactId, Date.now()]);
    res.json({ ok: true });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to add contact' });
  }
});

app.delete('/api/contacts/:id', requireAuth, async (req, res) => {
  try {
    const contactId = parseInt(req.params.id, 10);
    await run('DELETE FROM contacts WHERE user_id = ? AND contact_id = ?', [req.userId, contactId]);
    res.json({ ok: true });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: 'Failed to remove contact' });
  }
});

// Chats
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
        `SELECT * FROM messages WHERE chat_id = ? AND deleted = 0 ORDER BY created_at DESC LIMIT 1`,
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
              text: lastMessage.text,
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

    // Look for existing private chat
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
      'SELECT * FROM messages WHERE chat_id = ? AND deleted = 0 ORDER BY created_at ASC LIMIT 500',
      [chatId]
    );
    const messages = rows.map((m) => ({
      id: m.id,
      chat_id: m.chat_id,
      sender_id: m.sender_id,
      text: m.text,
      created_at: m.created_at,
      edited_at: m.edited_at,
      is_read: !!m.is_read,
      deleted: !!m.deleted
    }));
    res.json({ messages });
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
    res.json({ message });
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

// ========================================
// SOCKET.IO
// ========================================

const server = http.createServer(app);
const io = new Server(server, { cors: { origin: true, credentials: true } });

// userId -> Set of socket ids
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
    return true;
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

io.use(async (socket, next) => {
  const cookies = parseCookies(socket.handshake.headers.cookie);
  const token = cookies[SESSION_COOKIE];
  const userId = token ? await getUserIdBySession(token) : null;
  if (!userId) {
    return next(new Error('unauthorized'));
  }
  socket.userId = userId;
  next();
});

io.on('connection', async (socket) => {
  const userId = socket.userId;
  socket.join('user:' + userId);
  markOnline(userId, socket.id);

  await run('UPDATE users SET last_seen = ? WHERE id = ?', [Date.now(), userId]);
  socket.broadcast.emit('user_online', { user_id: userId });

  // Join all chat rooms
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
// FRONTEND - HTML, CSS, JS
// ========================================

function renderPage() {
  return `<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover, user-scalable=no">
<meta name="theme-color" content="#e53935">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Monogram</title>
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon-192.svg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
${renderCSS()}
</style>
</head>
<body>
<div id="toast-root" class="toast-root"></div>

<!-- Auth View -->
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

<!-- App View -->
<div id="app-view" class="app-view hidden">
  <!-- Main Content Area -->
  <div id="main-content" class="main-content">
    <!-- Chat List / Contacts / Profile / Settings sections -->
    <div id="section-chats" class="section active">
      <div class="section-header">
        <div class="search-wrap">
          <input id="search-input" type="text" placeholder="Search users...">
        </div>
      </div>
      <div id="search-results" class="search-results hidden"></div>
      <div id="chat-list" class="chat-list"></div>
    </div>

    <div id="section-contacts" class="section hidden">
      <div class="section-header">
        <h2>Contacts</h2>
        <button id="add-contact-btn" class="icon-btn" title="Add contact">+</button>
      </div>
      <div id="contact-list" class="contact-list"></div>
    </div>

    <div id="section-profile" class="section hidden">
      <div class="profile-view">
        <div class="profile-avatar-wrap">
          <span id="profile-avatar" class="avatar large"></span>
        </div>
        <div class="profile-info">
          <div id="profile-displayname" class="profile-name"></div>
          <div id="profile-username" class="profile-username"></div>
          <div id="profile-bio" class="profile-bio"></div>
          <div class="profile-meta">Joined <span id="profile-joined"></span></div>
        </div>
        <button id="profile-edit-btn" class="btn-secondary">Edit Profile</button>
        <button id="profile-logout-btn" class="btn-secondary logout-btn">Log Out</button>
      </div>
    </div>

    <div id="section-settings" class="section hidden">
      <div class="settings-view">
        <h2>Settings</h2>
        <div class="settings-group">
          <div class="settings-row">
            <span>Sound Effects</span>
            <label class="switch"><input type="checkbox" id="sound-toggle" checked><span class="switch-track"></span></label>
          </div>
          <div class="settings-row">
            <span>Volume</span>
            <input type="range" id="volume-slider" min="0" max="100" value="50">
          </div>
          <div class="settings-row">
            <span>Theme</span>
            <div class="theme-pick">
              <button class="theme-opt active" data-theme="dark">Dark</button>
              <button class="theme-opt" data-theme="light">Light</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Chat Panel (overlay on mobile) -->
  <div id="chat-panel" class="chat-panel hidden">
    <div class="chat-header">
      <button id="back-btn" class="icon-btn back-btn" title="Back">←</button>
      <span id="chat-avatar" class="avatar"></span>
      <div class="chat-header-info">
        <div id="chat-header-name" class="chat-header-name"></div>
        <div id="chat-header-status" class="chat-header-status"></div>
      </div>
    </div>
    <div id="messages" class="messages"></div>
    <div class="composer">
      <button id="emoji-btn" class="icon-btn" title="Emoji">😊</button>
      <div id="emoji-picker" class="emoji-picker hidden"></div>
      <textarea id="msg-input" rows="1" placeholder="Write a message..."></textarea>
      <button id="send-btn" class="send-btn" title="Send">➤</button>
    </div>
  </div>

  <!-- Bottom Navigation -->
  <nav id="bottom-nav" class="bottom-nav">
    <button class="nav-item active" data-section="chats">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none"><path d="M4 4h16v12H4V4z" stroke="currentColor" stroke-width="1.6"/><path d="M8 8h8M8 12h5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
      <span>Chats</span>
    </button>
    <button class="nav-item" data-section="contacts">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="8" r="3.5" stroke="currentColor" stroke-width="1.6"/><path d="M5 19c0-2.8 2.2-5 5-5h4c2.8 0 5 2.2 5 5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
      <span>Contacts</span>
    </button>
    <button class="nav-item" data-section="profile">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="8" r="3.5" stroke="currentColor" stroke-width="1.6"/><path d="M5 19c0-2.8 2.2-5 5-5h4c2.8 0 5 2.2 5 5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
      <span>Profile</span>
    </button>
    <button class="nav-item" data-section="settings">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none"><path d="M12 8.5a3.5 3.5 0 100 7 3.5 3.5 0 000-7z" stroke="currentColor" stroke-width="1.6"/><path d="M19 12a7 7 0 00-.1-1.2l1.9-1.5-1.5-2.6-2.2.8a7 7 0 00-2-1.2L14.8 4h-3l-.3 2.3a7 7 0 00-2 1.2l-2.2-.8-1.5 2.6 1.9 1.5A7 7 0 005 12c0 .4 0 .8.1 1.2l-1.9 1.5 1.5 2.6 2.2-.8c.6.5 1.3.9 2 1.2L9.2 20h3l.3-2.3c.7-.3 1.4-.7 2-1.2l2.2.8 1.5-2.6-1.9-1.5c.1-.4.1-.8.1-1.2z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>
      <span>Settings</span>
    </button>
  </nav>
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
  --bg: #0a0a0a;
  --surface: #141414;
  --surface-2: #1e1e1e;
  --border: #2a2a2a;
  --text: #f0f0f0;
  --text-dim: #888888;
  --accent: #e53935;
  --accent-hover: #d32f2f;
  --accent-glow: rgba(229, 57, 53, 0.4);
  --bubble-out: #e53935;
  --bubble-in: #1e1e1e;
  --radius: 16px;
  --nav-height: 64px;
  --safe-bottom: env(safe-area-inset-bottom, 0px);
  --font: 'Inter', 'Segoe UI', sans-serif;
}
html[data-theme="light"] {
  --bg: #f5f5f5;
  --surface: #ffffff;
  --surface-2: #eeeeee;
  --border: #dddddd;
  --text: #111111;
  --text-dim: #666666;
  --bubble-in: #eeeeee;
}
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; padding: 0; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--font);
  -webkit-font-smoothing: antialiased;
  overflow: hidden;
  padding-bottom: var(--safe-bottom);
}
.hidden { display: none !important; }
button { font-family: inherit; cursor: pointer; border: none; background: transparent; color: var(--text); }
input, textarea { font-family: inherit; background: var(--surface-2); color: var(--text); border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; outline: none; font-size: 14px; }
input:focus, textarea:focus { border-color: var(--accent); }
input[type="range"] { background: transparent; border: none; padding: 0; width: 100%; }

/* Toast */
.toast-root {
  position: fixed;
  top: 20px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 1000;
  display: flex;
  flex-direction: column;
  gap: 6px;
  align-items: center;
  pointer-events: none;
}
.toast {
  background: var(--surface);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 10px 18px;
  border-radius: 12px;
  font-size: 14px;
  box-shadow: 0 8px 30px rgba(0,0,0,0.3);
  animation: toastIn 0.25s ease;
  pointer-events: auto;
}
@keyframes toastIn { from { opacity: 0; transform: translateY(-10px); } to { opacity: 1; transform: translateY(0); } }

/* Auth */
.auth-view {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  animation: fadeIn 0.3s ease;
}
.auth-card {
  width: 100%;
  max-width: 380px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 20px;
  padding: 32px 24px;
}
.auth-brand { display: flex; align-items: center; gap: 12px; margin-bottom: 28px; }
.brand-mark {
  width: 44px; height: 44px; border-radius: 12px;
  background: var(--accent);
  display: flex; align-items: center; justify-content: center;
  font-weight: 700; font-size: 22px; color: white;
}
.brand-name { font-weight: 700; font-size: 22px; letter-spacing: -0.02em; }
.auth-tabs { display: flex; gap: 4px; background: var(--surface-2); padding: 4px; border-radius: 10px; margin-bottom: 20px; }
.auth-tab {
  flex: 1; padding: 10px 0; border-radius: 8px; background: transparent; color: var(--text-dim);
  font-weight: 600; font-size: 14px; transition: all 0.15s ease;
}
.auth-tab.active { background: var(--surface); color: var(--text); box-shadow: 0 1px 4px rgba(0,0,0,0.2); }
.auth-form { display: flex; flex-direction: column; gap: 14px; }
.field { display: flex; flex-direction: column; gap: 5px; font-size: 13px; color: var(--text-dim); }
.field input { padding: 12px 14px; }
.auth-error { color: var(--accent); font-size: 13px; min-height: 20px; }
.btn-primary {
  padding: 12px; border-radius: 12px; background: var(--accent); color: white; font-weight: 600; font-size: 16px;
  transition: background 0.15s ease, transform 0.1s ease;
}
.btn-primary:hover { background: var(--accent-hover); }
.btn-primary:active { transform: scale(0.97); }
.btn-secondary {
  padding: 10px 16px; border-radius: 10px; border: 1px solid var(--border); background: var(--surface-2);
  color: var(--text); font-weight: 500; font-size: 14px;
  transition: background 0.15s ease;
}
.btn-secondary:hover { background: var(--border); }
.logout-btn { color: var(--accent); border-color: var(--accent); }

/* App Layout */
.app-view {
  height: 100%;
  display: flex;
  flex-direction: column;
  position: relative;
  background: var(--bg);
}
.main-content {
  flex: 1;
  overflow: hidden;
  position: relative;
  padding-bottom: calc(var(--nav-height) + var(--safe-bottom));
}
.section {
  display: none;
  height: 100%;
  overflow-y: auto;
  padding: 16px;
}
.section.active { display: block; }
.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
  gap: 8px;
}
.section-header h2 { margin: 0; font-size: 20px; font-weight: 600; }
.search-wrap { flex: 1; }
#search-input { width: 100%; }

/* Bottom Navigation */
.bottom-nav {
  position: fixed;
  bottom: 0;
  left: 0;
  right: 0;
  height: calc(var(--nav-height) + var(--safe-bottom));
  background: var(--surface);
  border-top: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-around;
  padding-bottom: var(--safe-bottom);
  z-index: 10;
}
.nav-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  font-size: 10px;
  color: var(--text-dim);
  transition: color 0.15s ease;
  padding: 4px 12px;
}
.nav-item.active { color: var(--accent); }
.nav-item svg { width: 24px; height: 24px; stroke: currentColor; fill: none; }
.nav-item span { font-weight: 500; }

/* Chat List */
.chat-list, .contact-list { display: flex; flex-direction: column; gap: 4px; }
.chat-row, .contact-row {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 12px 14px;
  background: var(--surface);
  border-radius: 12px;
  cursor: pointer;
  transition: background 0.12s ease;
}
.chat-row:hover, .contact-row:hover { background: var(--surface-2); }
.chat-row-body, .contact-row-body { flex: 1; min-width: 0; }
.chat-row-top, .contact-row-top { display: flex; justify-content: space-between; align-items: baseline; gap: 6px; }
.chat-row-name, .contact-row-name { font-weight: 600; font-size: 15px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.chat-row-time, .contact-row-info { font-size: 12px; color: var(--text-dim); }
.chat-row-bottom { display: flex; justify-content: space-between; align-items: center; gap: 6px; margin-top: 4px; }
.chat-row-preview { font-size: 13px; color: var(--text-dim); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.unread-badge {
  background: var(--accent); color: white; font-size: 11px; font-weight: 700;
  min-width: 18px; height: 18px; border-radius: 9px;
  display: flex; align-items: center; justify-content: center; padding: 0 6px;
}
.empty-hint { padding: 40px 16px; text-align: center; color: var(--text-dim); font-size: 15px; }

/* Chat Panel */
.chat-panel {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: var(--bg);
  display: flex;
  flex-direction: column;
  z-index: 20;
  transform: translateX(100%);
  transition: transform 0.25s ease;
}
.chat-panel.open { transform: translateX(0); }
.chat-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
}
.chat-header-info { flex: 1; min-width: 0; }
.chat-header-name { font-weight: 600; font-size: 16px; }
.chat-header-status { font-size: 12px; color: var(--text-dim); }
.chat-header-status.online { color: #4caf50; }
.back-btn { font-size: 22px; padding: 0 4px; }
.messages {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.msg-row { display: flex; margin-top: 8px; animation: msgIn 0.18s ease; }
.msg-row.out { justify-content: flex-end; }
.msg-row.in { justify-content: flex-start; }
@keyframes msgIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
.bubble {
  max-width: 75%;
  padding: 8px 14px;
  border-radius: 16px;
  font-size: 14.5px;
  line-height: 1.4;
  word-wrap: break-word;
}
.msg-row.in .bubble { background: var(--bubble-in); border-bottom-left-radius: 4px; }
.msg-row.out .bubble { background: var(--bubble-out); color: white; border-bottom-right-radius: 4px; }
.bubble-meta {
  display: flex;
  gap: 5px;
  align-items: center;
  margin-top: 4px;
  font-size: 10.5px;
  opacity: 0.7;
  justify-content: flex-end;
}
.bubble.deleted { font-style: italic; opacity: 0.5; }
.typing-indicator { font-size: 13px; color: var(--text-dim); padding: 4px 6px; font-style: italic; }
.composer {
  display: flex;
  align-items: flex-end;
  gap: 8px;
  padding: 10px 14px;
  background: var(--surface);
  border-top: 1px solid var(--border);
}
#msg-input {
  flex: 1;
  resize: none;
  max-height: 120px;
  padding: 10px 14px;
  border-radius: 20px;
  border: 1px solid var(--border);
  background: var(--surface-2);
  color: var(--text);
  font-size: 14px;
  outline: none;
  line-height: 1.4;
}
#msg-input:focus { border-color: var(--accent); }
.send-btn {
  width: 40px; height: 40px; border-radius: 50%;
  background: var(--accent); color: white;
  display: flex; align-items: center; justify-content: center;
  transition: background 0.15s ease, transform 0.1s ease;
}
.send-btn:hover { background: var(--accent-hover); }
.send-btn:active { transform: scale(0.92); }
.emoji-picker {
  position: absolute;
  bottom: 60px;
  left: 12px;
  width: 240px;
  max-height: 180px;
  overflow-y: auto;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 8px;
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  gap: 4px;
  box-shadow: 0 8px 30px rgba(0,0,0,0.4);
  z-index: 30;
}
.emoji-picker button { font-size: 20px; padding: 4px; border-radius: 6px; background: transparent; }
.emoji-picker button:hover { background: var(--surface-2); }

/* Profile View */
.profile-view {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 16px;
  padding: 20px 0;
}
.profile-avatar-wrap .avatar.large { width: 96px; height: 96px; font-size: 32px; }
.profile-info { text-align: center; }
.profile-name { font-size: 22px; font-weight: 700; }
.profile-username { font-size: 15px; color: var(--text-dim); }
.profile-bio { font-size: 14px; color: var(--text-dim); margin-top: 6px; max-width: 300px; }
.profile-meta { font-size: 13px; color: var(--text-dim); margin-top: 8px; }

/* Settings */
.settings-view { padding: 16px 0; }
.settings-view h2 { margin-bottom: 20px; }
.settings-group { display: flex; flex-direction: column; gap: 12px; }
.settings-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 0;
  border-bottom: 1px solid var(--border);
  font-size: 15px;
}
.settings-row:last-child { border-bottom: none; }
.settings-row input[type="range"] { width: 120px; }
.theme-pick { display: flex; gap: 6px; }
.theme-opt {
  padding: 6px 14px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--surface-2);
  color: var(--text-dim);
  font-weight: 600;
  font-size: 13px;
}
.theme-opt.active { background: var(--accent); color: white; border-color: var(--accent); }
.switch {
  position: relative;
  width: 44px;
  height: 24px;
}
.switch input { opacity: 0; width: 0; height: 0; }
.switch-track {
  position: absolute;
  inset: 0;
  background: var(--border);
  border-radius: 24px;
  transition: background 0.15s ease;
}
.switch-track::before {
  content: "";
  position: absolute;
  width: 18px;
  height: 18px;
  left: 3px;
  top: 3px;
  background: white;
  border-radius: 50%;
  transition: transform 0.15s ease;
}
.switch input:checked + .switch-track { background: var(--accent); }
.switch input:checked + .switch-track::before { transform: translateX(20px); }

/* Avatar */
.avatar {
  width: 42px;
  height: 42px;
  border-radius: 50%;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 600;
  font-size: 16px;
  color: white;
  background-size: cover;
  background-position: center;
}
.avatar.large { width: 72px; height: 72px; font-size: 28px; }

/* Scrollbar */
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 8px; }

/* Responsive */
@media (min-width: 600px) {
  .chat-panel {
    position: relative;
    transform: translateX(0) !important;
    left: auto;
    right: auto;
    bottom: auto;
    top: auto;
    width: 50%;
    border-left: 1px solid var(--border);
    background: var(--surface);
  }
  .main-content {
    display: flex;
    padding-bottom: 0;
  }
  .section {
    flex: 1;
    overflow-y: auto;
    padding: 16px;
    border-right: 1px solid var(--border);
  }
  .section.active {
    display: block;
  }
  .bottom-nav {
    display: none;
  }
  .app-view {
    flex-direction: row;
  }
  .chat-panel {
    flex: 1;
    border-left: 1px solid var(--border);
  }
  .back-btn { display: none; }
}
@media (max-width: 599px) {
  .section { padding: 12px; }
  .bottom-nav { display: flex; }
}
`;
}

// ========================================
// CLIENT JAVASCRIPT
// ========================================

function renderClientJS() {
  return `
(function(){
  'use strict';

  // ----- STATE -----
  const state = {
    user: null,
    chats: [],
    chatsById: {},
    currentChatId: null,
    messagesCache: {},
    socket: null,
    typingTimer: null,
    soundOn: true,
    volume: 0.5,
    theme: 'dark',
    currentSection: 'chats',
    contacts: [],
    editingMessageId: null,
  };

  // ----- DOM REFS -----
  const $ = id => document.getElementById(id);
  const qs = (sel, ctx) => (ctx || document).querySelector(sel);
  const qsa = (sel, ctx) => (ctx || document).querySelectorAll(sel);

  // ----- UTILITIES -----
  function toast(msg) {
    const root = $('toast-root');
    const el = document.createElement('div');
    el.className = 'toast';
    el.textContent = msg;
    root.appendChild(el);
    setTimeout(() => el.remove(), 3000);
  }

  function initFor(name) {
    const parts = (name || '?').trim().split(/\\s+/);
    if (parts.length === 1) return parts[0].charAt(0).toUpperCase();
    return (parts[0].charAt(0) + parts[1].charAt(0)).toUpperCase();
  }

  function hueFor(str) {
    let hash = 0;
    for (let i = 0; i < str.length; i++) {
      hash = (hash << 5) - hash + str.charCodeAt(i);
      hash |= 0;
    }
    return Math.abs(hash) % 360;
  }

  function paintAvatar(el, user) {
    el.textContent = '';
    el.style.background = '';
    if (user && user.avatar) {
      el.style.background = 'url(' + user.avatar + ') center/cover no-repeat';
    } else {
      const name = user ? (user.display_name || user.username || '?') : '?';
      const hue = hueFor(user ? (user.username || name) : 'x');
      el.style.background = 'linear-gradient(135deg, hsl('+hue+',70%,55%), hsl('+((hue+40)%360)+',70%,45%))';
      el.textContent = initFor(name);
    }
  }

  function timeLabel(ts) {
    const d = new Date(ts);
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    const h = String(d.getHours()).padStart(2,'0');
    const m = String(d.getMinutes()).padStart(2,'0');
    if (sameDay) return h+':'+m;
    return (d.getMonth()+1)+'/'+d.getDate();
  }

  function lastSeenLabel(ts) {
    if (!ts) return 'offline';
    const diff = Date.now() - ts;
    if (diff < 60000) return 'last seen just now';
    if (diff < 3600000) return 'last seen recently';
    if (diff < 86400000) return 'last seen today';
    return 'last seen recently';
  }

  // ----- API -----
  function api(method, url, body) {
    const opts = {
      method,
      headers: {'Content-Type':'application/json'},
      credentials: 'same-origin'
    };
    if (body !== undefined) opts.body = JSON.stringify(body);
    return fetch(url, opts).then(res => {
      return res.json().then(data => {
        if (!res.ok) {
          const err = new Error(data && data.error ? data.error : 'Request failed');
          throw err;
        }
        return data;
      });
    });
  }

  // ----- SOUND (Web Audio) -----
  let audioCtx = null;
  function playSound(type) {
    if (!state.soundOn) return;
    try {
      if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.type = 'sine';
      let freq = 600, dur = 0.08, vol = 0.05;
      if (type === 'send') { freq = 800; dur = 0.06; vol = 0.06; }
      else if (type === 'receive') { freq = 500; dur = 0.1; vol = 0.08; }
      else if (type === 'notify') { freq = 880; dur = 0.15; vol = 0.1; }
      else if (type === 'error') { freq = 300; dur = 0.15; vol = 0.1; }
      else if (type === 'click') { freq = 700; dur = 0.04; vol = 0.04; }
      osc.frequency.value = freq;
      gain.gain.value = vol * state.volume;
      osc.connect(gain);
      gain.connect(audioCtx.destination);
      osc.start();
      gain.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + dur);
      osc.stop(audioCtx.currentTime + dur);
    } catch(e) { /* ignore */ }
  }

  // ----- THEME -----
  function applyTheme(theme) {
    state.theme = theme;
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('mg_theme', theme);
    qsa('.theme-opt').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.theme === theme);
    });
  }

  // ----- AUTH VIEWS -----
  function showAuth() {
    $('auth-view').classList.remove('hidden');
    $('app-view').classList.add('hidden');
  }

  function showApp() {
    $('auth-view').classList.add('hidden');
    $('app-view').classList.remove('hidden');
    loadChats();
    loadContacts();
  }

  // ----- NAVIGATION -----
  function navigateTo(section) {
    state.currentSection = section;
    qsa('.section').forEach(el => el.classList.remove('active'));
    const target = document.getElementById('section-' + section);
    if (target) target.classList.add('active');
    qsa('.nav-item').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.section === section);
    });
    // Refresh content when switching
    if (section === 'chats') loadChats();
    else if (section === 'contacts') loadContacts();
    else if (section === 'profile') loadProfile();
  }

  // ----- CHATS -----
  async function loadChats() {
    try {
      const data = await api('GET', '/api/chats');
      state.chats = data.chats;
      state.chatsById = {};
      data.chats.forEach(c => state.chatsById[c.id] = c);
      renderChatList();
    } catch(e) { toast(e.message); }
  }

  function renderChatList() {
    const container = $('chat-list');
    container.textContent = '';
    if (!state.chats.length) {
      const hint = document.createElement('div');
      hint.className = 'empty-hint';
      hint.textContent = 'No chats yet. Search for someone to start messaging.';
      container.appendChild(hint);
      return;
    }
    state.chats.forEach(chat => {
      const row = document.createElement('div');
      row.className = 'chat-row' + (chat.id === state.currentChatId ? ' active' : '');
      const avatar = document.createElement('span');
      avatar.className = 'avatar';
      paintAvatar(avatar, chat.peer);
      const body = document.createElement('div');
      body.className = 'chat-row-body';
      const top = document.createElement('div');
      top.className = 'chat-row-top';
      const name = document.createElement('div');
      name.className = 'chat-row-name';
      name.textContent = chat.peer ? chat.peer.display_name : 'Unknown';
      top.appendChild(name);
      if (chat.last_message) {
        const time = document.createElement('span');
        time.className = 'chat-row-time';
        time.textContent = timeLabel(chat.last_message.created_at);
        top.appendChild(time);
      }
      const bottom = document.createElement('div');
      bottom.className = 'chat-row-bottom';
      const preview = document.createElement('div');
      preview.className = 'chat-row-preview';
      if (chat.last_message) {
        const prefix = chat.last_message.sender_id === state.user.id ? 'You: ' : '';
        preview.textContent = prefix + chat.last_message.text;
      } else {
        preview.textContent = 'Say hello 👋';
      }
      bottom.appendChild(preview);
      if (chat.unread_count > 0) {
        const badge = document.createElement('span');
        badge.className = 'unread-badge';
        badge.textContent = chat.unread_count > 99 ? '99+' : String(chat.unread_count);
        bottom.appendChild(badge);
      }
      body.appendChild(top);
      body.appendChild(bottom);
      row.appendChild(avatar);
      row.appendChild(body);
      row.addEventListener('click', () => openChat(chat.id));
      container.appendChild(row);
    });
  }

  // ----- CHAT PANEL -----
  async function openChat(chatId) {
    state.currentChatId = chatId;
    const panel = $('chat-panel');
    panel.classList.add('open');
    const chat = state.chatsById[chatId];
    if (chat) {
      paintAvatar($('chat-avatar'), chat.peer);
      $('chat-header-name').textContent = chat.peer ? chat.peer.display_name : 'Unknown';
      setChatStatus(chat.peer ? chat.peer.last_seen : null, chat._online || false);
    }
    if (state.socket) state.socket.emit('join_chat', chatId);
    try {
      const data = await api('GET', '/api/chats/' + chatId + '/messages');
      state.messagesCache[chatId] = data.messages;
      renderMessages(chatId);
      if (state.socket) state.socket.emit('message_read', chatId);
    } catch(e) { toast(e.message); }
  }

  function setChatStatus(lastSeen, online) {
    const status = $('chat-header-status');
    if (online) {
      status.textContent = 'Online';
      status.classList.add('online');
    } else {
      status.textContent = lastSeenLabel(lastSeen);
      status.classList.remove('online');
    }
  }

  function renderMessages(chatId) {
    const container = $('messages');
    container.textContent = '';
    const list = state.messagesCache[chatId] || [];
    list.forEach(m => {
      const row = document.createElement('div');
      row.className = 'msg-row ' + (m.sender_id === state.user.id ? 'out' : 'in');
      const bubble = document.createElement('div');
      bubble.className = 'bubble' + (m.deleted ? ' deleted' : '');
      bubble.textContent = m.deleted ? 'Message deleted' : m.text;
      const meta = document.createElement('div');
      meta.className = 'bubble-meta';
      let metaText = timeLabel(m.created_at);
      if (m.edited_at) metaText += ' · edited';
      if (m.sender_id === state.user.id && !m.deleted) {
        metaText += m.is_read ? ' · ✓✓' : ' · ✓';
      }
      meta.textContent = metaText;
      bubble.appendChild(meta);
      // Actions (for own messages)
      if (m.sender_id === state.user.id && !m.deleted) {
        const actions = document.createElement('div');
        actions.className = 'bubble-actions';
        const editBtn = document.createElement('button');
        editBtn.textContent = 'Edit';
        editBtn.onclick = (e) => { e.stopPropagation(); startEdit(m); };
        const delBtn = document.createElement('button');
        delBtn.textContent = 'Delete';
        delBtn.onclick = (e) => { e.stopPropagation(); deleteMessage(m); };
        actions.appendChild(editBtn);
        actions.appendChild(delBtn);
        row.appendChild(actions);
      }
      row.appendChild(bubble);
      container.appendChild(row);
    });
    container.scrollTop = container.scrollHeight;
  }

  function startEdit(m) {
    state.editingMessageId = m.id;
    $('msg-input').value = m.text;
    $('msg-input').focus();
    toast('Editing message — press Enter to save');
  }

  async function deleteMessage(m) {
    if (!confirm('Delete this message?')) return;
    try {
      await api('DELETE', '/api/messages/' + m.id);
      m.deleted = true;
      renderMessages(state.currentChatId);
      loadChats();
    } catch(e) { toast(e.message); }
  }

  // ----- COMPOSER -----
  const msgInput = $('msg-input');
  msgInput.addEventListener('input', function() {
    this.style.height = 'auto';
    this.style.height = Math.min(this.scrollHeight, 120) + 'px';
    if (!state.currentChatId || !state.socket) return;
    state.socket.emit('typing', state.currentChatId);
    clearTimeout(state.typingTimer);
    state.typingTimer = setTimeout(() => {
      state.socket.emit('stop_typing', state.currentChatId);
    }, 1200);
  });

  msgInput.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  $('send-btn').addEventListener('click', sendMessage);

  async function sendMessage() {
    const text = msgInput.value.trim();
    if (!text || !state.currentChatId) return;
    if (state.editingMessageId) {
      const id = state.editingMessageId;
      state.editingMessageId = null;
      try {
        await api('PUT', '/api/messages/' + id, { text });
        const list = state.messagesCache[state.currentChatId] || [];
        const m = list.find(x => x.id === id);
        if (m) { m.text = text; m.edited_at = Date.now(); }
        renderMessages(state.currentChatId);
        loadChats();
        playSound('send');
      } catch(e) { toast(e.message); }
      msgInput.value = '';
      msgInput.style.height = 'auto';
      return;
    }
    msgInput.value = '';
    msgInput.style.height = 'auto';
    if (state.socket) state.socket.emit('stop_typing', state.currentChatId);
    try {
      const data = await api('POST', '/api/messages', { chatId: state.currentChatId, text });
      const list = state.messagesCache[state.currentChatId] || [];
      list.push(data.message);
      renderMessages(state.currentChatId);
      loadChats();
      playSound('send');
    } catch(e) { toast(e.message); }
  }

  // ----- EMOJI PICKER -----
  const EMOJIS = ['😀','😂','😍','😊','😉','😎','🤔','😢','😭','😡','👍','👎','🙏','👏','🔥','🎉','❤️','💯','✨','😴','🥳','😱','🤝','👌','🙌','🤷','😅','🙂','😇','🤩'];
  const picker = $('emoji-picker');
  EMOJIS.forEach(e => {
    const btn = document.createElement('button');
    btn.textContent = e;
    btn.onclick = () => {
      msgInput.value += e;
      msgInput.focus();
    };
    picker.appendChild(btn);
  });
  $('emoji-btn').addEventListener('click', e => {
    e.stopPropagation();
    picker.classList.toggle('hidden');
  });
  document.addEventListener('click', () => picker.classList.add('hidden'));

  // ----- BACK BUTTON (mobile) -----
  $('back-btn').addEventListener('click', () => {
    $('chat-panel').classList.remove('open');
    state.currentChatId = null;
  });

  // ----- CONTACTS -----
  async function loadContacts() {
    try {
      const data = await api('GET', '/api/contacts');
      state.contacts = data.contacts || [];
      renderContacts();
    } catch(e) { toast(e.message); }
  }

  function renderContacts() {
    const container = $('contact-list');
    container.textContent = '';
    if (!state.contacts.length) {
      const hint = document.createElement('div');
      hint.className = 'empty-hint';
      hint.textContent = 'No contacts yet. Add someone by searching in Chats.';
      container.appendChild(hint);
      return;
    }
    state.contacts.forEach(contact => {
      const row = document.createElement('div');
      row.className = 'contact-row';
      const avatar = document.createElement('span');
      avatar.className = 'avatar';
      paintAvatar(avatar, contact);
      const body = document.createElement('div');
      body.className = 'contact-row-body';
      const top = document.createElement('div');
      top.className = 'contact-row-top';
      const name = document.createElement('div');
      name.className = 'contact-row-name';
      name.textContent = contact.display_name;
      top.appendChild(name);
      const info = document.createElement('span');
      info.className = 'contact-row-info';
      info.textContent = '@' + contact.username;
      top.appendChild(info);
      body.appendChild(top);
      const actions = document.createElement('div');
      actions.style.display = 'flex';
      actions.style.gap = '8px';
      const chatBtn = document.createElement('button');
      chatBtn.className = 'btn-secondary';
      chatBtn.textContent = 'Message';
      chatBtn.onclick = (e) => { e.stopPropagation(); startChatWith(contact.id); };
      const removeBtn = document.createElement('button');
      removeBtn.className = 'btn-secondary logout-btn';
      removeBtn.textContent = 'Remove';
      removeBtn.onclick = async (e) => {
        e.stopPropagation();
        if (!confirm('Remove contact?')) return;
        try {
          await api('DELETE', '/api/contacts/' + contact.id);
          loadContacts();
          toast('Contact removed');
        } catch(e) { toast(e.message); }
      };
      actions.appendChild(chatBtn);
      actions.appendChild(removeBtn);
      body.appendChild(actions);
      row.appendChild(avatar);
      row.appendChild(body);
      container.appendChild(row);
    });
  }

  async function startChatWith(userId) {
    try {
      const data = await api('POST', '/api/chats', { userId });
      const chat = data.chat;
      state.chats.unshift(chat);
      state.chatsById[chat.id] = chat;
      navigateTo('chats');
      loadChats();
      openChat(chat.id);
    } catch(e) { toast(e.message); }
  }

  $('add-contact-btn').addEventListener('click', () => {
    const username = prompt('Enter username to add as contact:');
    if (!username) return;
    // Search user by username
    api('GET', '/api/users/search?q=' + encodeURIComponent(username))
      .then(data => {
        if (!data.users || !data.users.length) return toast('User not found');
        const user = data.users[0];
        return api('POST', '/api/contacts', { contactId: user.id })
          .then(() => { toast('Contact added'); loadContacts(); })
          .catch(e => toast(e.message));
      })
      .catch(e => toast(e.message));
  });

  // ----- PROFILE -----
  function loadProfile() {
    const user = state.user;
    if (!user) return;
    paintAvatar($('profile-avatar'), user);
    $('profile-displayname').textContent = user.display_name;
    $('profile-username').textContent = '@' + user.username;
    $('profile-bio').textContent = user.bio || 'No bio yet.';
    $('profile-joined').textContent = new Date(user.created_at).toLocaleDateString();
  }

  $('profile-edit-btn').addEventListener('click', () => {
    const user = state.user;
    const newDisplay = prompt('Display name:', user.display_name);
    if (newDisplay !== null && newDisplay.trim()) {
      const newBio = prompt('Bio:', user.bio || '');
      const newAvatar = prompt('Avatar URL (leave blank to keep):', user.avatar || '');
      api('PUT', '/api/profile', {
        display_name: newDisplay.trim(),
        bio: newBio !== null ? newBio.trim() : undefined,
        avatar: newAvatar !== null ? newAvatar.trim() : undefined
      }).then(data => {
        state.user = data.user;
        loadProfile();
        toast('Profile updated');
      }).catch(e => toast(e.message));
    }
  });

  $('profile-logout-btn').addEventListener('click', async () => {
    try {
      await api('POST', '/api/logout');
      if (state.socket) state.socket.disconnect();
      state.user = null;
      state.chats = [];
      state.currentChatId = null;
      $('chat-panel').classList.remove('open');
      showAuth();
    } catch(e) { toast(e.message); }
  });

  // ----- SETTINGS -----
  $('sound-toggle').addEventListener('change', function() {
    state.soundOn = this.checked;
    localStorage.setItem('mg_sound', this.checked ? '1' : '0');
  });
  $('volume-slider').addEventListener('input', function() {
    state.volume = parseInt(this.value) / 100;
    localStorage.setItem('mg_volume', String(state.volume));
  });
  qsa('.theme-opt').forEach(btn => {
    btn.addEventListener('click', () => {
      applyTheme(btn.dataset.theme);
    });
  });

  // ----- SOCKET.IO -----
  function connectSocket() {
    if (state.socket) state.socket.disconnect();
    state.socket = io({ withCredentials: true });

    state.socket.on('new_message', (msg) => {
      cacheMessage(msg);
      if (msg.chat_id === state.currentChatId) {
        renderMessages(msg.chat_id);
        if (msg.sender_id !== state.user.id) {
          state.socket.emit('message_read', state.currentChatId);
          playSound('receive');
        }
      } else if (msg.sender_id !== state.user.id) {
        playSound('notify');
        // Update unread badge and chat list
        loadChats();
      }
      loadChats();
    });

    state.socket.on('chat_updated', () => { loadChats(); });

    state.socket.on('typing', (data) => {
      if (data.chat_id === state.currentChatId && data.user_id !== state.user.id) {
        showTyping(true);
      }
    });
    state.socket.on('stop_typing', (data) => {
      if (data.chat_id === state.currentChatId && data.user_id !== state.user.id) {
        showTyping(false);
      }
    });

    state.socket.on('message_read', (data) => {
      if (data.chat_id === state.currentChatId) {
        const list = state.messagesCache[data.chat_id] || [];
        list.forEach(m => { if (m.sender_id === state.user.id) m.is_read = true; });
        renderMessages(data.chat_id);
      }
    });

    state.socket.on('message_edited', (data) => {
      const list = state.messagesCache[data.chat_id] || [];
      const m = list.find(x => x.id === data.id);
      if (m) { m.text = data.text; m.edited_at = data.edited_at; }
      if (data.chat_id === state.currentChatId) renderMessages(data.chat_id);
      loadChats();
    });

    state.socket.on('message_deleted', (data) => {
      const list = state.messagesCache[data.chat_id] || [];
      const m = list.find(x => x.id === data.id);
      if (m) { m.deleted = true; m.text = ''; }
      if (data.chat_id === state.currentChatId) renderMessages(data.chat_id);
      loadChats();
    });

    state.socket.on('user_online', (data) => updatePeerStatus(data.user_id, true, null));
    state.socket.on('user_offline', (data) => updatePeerStatus(data.user_id, false, data.last_seen));
  }

  function cacheMessage(msg) {
    if (!state.messagesCache[msg.chat_id]) state.messagesCache[msg.chat_id] = [];
    const list = state.messagesCache[msg.chat_id];
    const existing = list.findIndex(x => x.id === msg.id);
    if (existing !== -1) list[existing] = msg;
    else list.push(msg);
  }

  function updatePeerStatus(userId, online, lastSeen) {
    state.chats.forEach(chat => {
      if (chat.peer && chat.peer.id === userId) {
        chat._online = online;
        if (lastSeen) chat.peer.last_seen = lastSeen;
        if (chat.id === state.currentChatId) {
          setChatStatus(chat.peer.last_seen, online);
        }
      }
    });
    // Also update contacts if needed
  }

  let typingTimeout = null;
  function showTyping(on) {
    const existing = document.getElementById('typing-indicator');
    if (on) {
      if (existing) return;
      const el = document.createElement('div');
      el.id = 'typing-indicator';
      el.className = 'typing-indicator';
      el.textContent = 'typing...';
      $('messages').appendChild(el);
      $('messages').scrollTop = $('messages').scrollHeight;
    } else {
      if (existing) existing.remove();
    }
  }

  // ----- SEARCH (global) -----
  let searchDebounce = null;
  $('search-input').addEventListener('input', function() {
    const q = this.value.trim();
    clearTimeout(searchDebounce);
    if (!q) {
      $('search-results').classList.add('hidden');
      $('chat-list').classList.remove('hidden');
      return;
    }
    searchDebounce = setTimeout(() => {
      api('GET', '/api/users/search?q=' + encodeURIComponent(q))
        .then(data => {
          renderSearchResults(data.users || []);
        })
        .catch(() => {});
    }, 300);
  });

  function renderSearchResults(users) {
    const container = $('search-results');
    container.textContent = '';
    $('chat-list').classList.add('hidden');
    container.classList.remove('hidden');
    if (!users.length) {
      const hint = document.createElement('div');
      hint.className = 'empty-hint';
      hint.textContent = 'No users found.';
      container.appendChild(hint);
      return;
    }
    users.forEach(u => {
      const row = document.createElement('div');
      row.className = 'contact-row';
      const avatar = document.createElement('span');
      avatar.className = 'avatar';
      paintAvatar(avatar, u);
      const body = document.createElement('div');
      body.className = 'contact-row-body';
      const top = document.createElement('div');
      top.className = 'contact-row-top';
      const name = document.createElement('div');
      name.className = 'contact-row-name';
      name.textContent = u.display_name;
      top.appendChild(name);
      const uname = document.createElement('span');
      uname.className = 'contact-row-info';
      uname.textContent = '@' + u.username;
      top.appendChild(uname);
      body.appendChild(top);
      const actions = document.createElement('div');
      actions.style.display = 'flex';
      actions.style.gap = '8px';
      const chatBtn = document.createElement('button');
      chatBtn.className = 'btn-secondary';
      chatBtn.textContent = 'Message';
      chatBtn.onclick = (e) => {
        e.stopPropagation();
        startChatWith(u.id);
      };
      const addBtn = document.createElement('button');
      addBtn.className = 'btn-secondary';
      addBtn.textContent = 'Add';
      addBtn.onclick = async (e) => {
        e.stopPropagation();
        try {
          await api('POST', '/api/contacts', { contactId: u.id });
          toast('Contact added');
          $('search-input').value = '';
          $('search-results').classList.add('hidden');
          $('chat-list').classList.remove('hidden');
          loadContacts();
        } catch(e) { toast(e.message); }
      };
      actions.appendChild(chatBtn);
      actions.appendChild(addBtn);
      body.appendChild(actions);
      row.appendChild(avatar);
      row.appendChild(body);
      container.appendChild(row);
    });
  }

  // ----- NAVIGATION EVENTS -----
  qsa('.nav-item').forEach(btn => {
    btn.addEventListener('click', () => {
      const section = btn.dataset.section;
      navigateTo(section);
    });
  });

  // ----- INIT -----
  async function init() {
    const savedTheme = localStorage.getItem('mg_theme') || 'dark';
    applyTheme(savedTheme);
    const savedSound = localStorage.getItem('mg_sound');
    state.soundOn = savedSound === null ? true : savedSound === '1';
    $('sound-toggle').checked = state.soundOn;
    const savedVolume = parseFloat(localStorage.getItem('mg_volume'));
    state.volume = isNaN(savedVolume) ? 0.5 : Math.min(1, Math.max(0, savedVolume));
    $('volume-slider').value = state.volume * 100;

    try {
      const data = await api('GET', '/api/me');
      state.user = data.user;
      showApp();
      connectSocket();
      navigateTo('chats');
    } catch(e) {
      showAuth();
    }

    // Auth forms
    $('login-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const username = $('login-username').value.trim();
      const password = $('login-password').value;
      try {
        const data = await api('POST', '/api/login', { username, password });
        state.user = data.user;
        showApp();
        connectSocket();
        navigateTo('chats');
      } catch(err) {
        $('login-error').textContent = err.message;
      }
    });

    $('register-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const username = $('reg-username').value.trim();
      const displayName = $('reg-displayname').value.trim() || username;
      const password = $('reg-password').value;
      try {
        const data = await api('POST', '/api/register', { username, display_name: displayName, password });
        state.user = data.user;
        showApp();
        connectSocket();
        navigateTo('chats');
      } catch(err) {
        $('register-error').textContent = err.message;
      }
    });

    qsa('.auth-tab').forEach(tab => {
      tab.addEventListener('click', () => {
        qsa('.auth-tab').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        const which = tab.dataset.tab;
        $('login-form').classList.toggle('hidden', which !== 'login');
        $('register-form').classList.toggle('hidden', which !== 'register');
      });
    });

    // Close chat panel on outside click (desktop)
    document.addEventListener('click', (e) => {
      if (window.innerWidth >= 600) return;
      const panel = $('chat-panel');
      if (panel.classList.contains('open') && !panel.contains(e.target) && !e.target.closest('.chat-row')) {
        panel.classList.remove('open');
        state.currentChatId = null;
      }
    });
  }

  init();
})();
`;
}

// ========================================
// SERVER START
// ========================================

app.get('/', (req, res) => {
  res.send(renderPage());
});

// 404 handler
app.use((req, res) => {
  res.status(404).json({ error: 'Not found' });
});

// Error handler
app.use((err, req, res, next) => {
  console.error(err);
  res.status(500).json({ error: 'Internal server error' });
});

initDatabase();

server.listen(PORT, HOST, () => {
  console.log('========================================');
  console.log('  Monogram is running');
  console.log('  http://' + HOST + ':' + PORT);
  console.log('  Database: ' + DB_PATH);
  console.log('========================================');
});
