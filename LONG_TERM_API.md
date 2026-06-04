# Long-Term Key Management API Documentation

## Overview

This API provides endpoints for managing API keys in long-term monitoring storage. All endpoints (except `/auth` and `/check-duplicate`) require admin authentication via JWT Bearer token.

**Base URL:** `/api/long-term`

**Admin Password:** `bingxujingAb` (configure via `ADMIN_PASSWORD` environment variable in production)

---

## Authentication

### POST `/auth`

Authenticate admin and receive JWT token.

**Request:**
```json
{
  "password": "bingxujingAb"
}
```

**Response:**
```json
{
  "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "expires_in": 86400
}
```

**Usage:**
```javascript
const response = await fetch('/api/long-term/auth', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ password: 'bingxujingAb' })
});
const { token } = await response.json();
```

---

## Key Management

### GET `/keys`

List keys with filtering and pagination.

**Authentication:** Required

**Query Parameters:**
- `platform` (optional): Filter by platform (`gemini`, `openai`, `anthropic`, `gcp`)
- `status` (optional): Filter by status (`active`, `dead`, `abandoned`)
- `search` (optional): Search in notes or masked key
- `limit` (default: 100, max: 1000): Number of results
- `offset` (default: 0): Pagination offset

**Response:**
```json
{
  "keys": [
    {
      "id": 1,
      "masked_key": "AIzaSy***************abc",
      "platform": "gemini",
      "status": "active",
      "last_check": 1717654321.0,
      "error_code": null,
      "death_time": null,
      "retry_count": 0,
      "created_at": 1717550000.0,
      "notes": "Production key",
      "next_check_time": 1717740721.0
    }
  ],
  "total": 42
}
```

**Usage:**
```javascript
const response = await fetch('/api/long-term/keys?platform=gemini&status=active&limit=50', {
  headers: { 'Authorization': `Bearer ${token}` }
});
const { keys, total } = await response.json();
```

---

### POST `/keys`

Add keys to long-term monitoring (batch).

**Authentication:** Required

**Request:**
```json
{
  "keys": [
    "AIzaSyABC...",
    "sk-proj-XYZ..."
  ],
  "platform": "gemini",
  "notes": "Batch import from production"
}
```

**Response:**
```json
{
  "added": 2,
  "duplicates": 0,
  "key_ids": [123, 124]
}
```

---

### POST `/keys/move`

Move keys from short-term to long-term storage.

**Authentication:** Required

**Request:**
```json
{
  "keys": ["AIzaSyABC...", "sk-proj-XYZ..."],
  "platform": "gemini",
  "notes": "Moved from short-term"
}
```

**Response:** Same as `/keys` (AddKeysResponse)

---

### POST `/keys/{key_id}/check`

Manually check a single key.

**Authentication:** Required

**Response:**
```json
{
  "key_id": 123,
  "status": "active",
  "error_class": null,
  "error_detail": null,
  "response_time_ms": 234.5,
  "checked_at": 1717654321.0
}
```

**Usage:**
```javascript
const response = await fetch('/api/long-term/keys/123/check', {
  method: 'POST',
  headers: { 'Authorization': `Bearer ${token}` }
});
const result = await response.json();
```

---

### POST `/keys/check`

Batch check multiple keys or all keys.

**Authentication:** Required

**Request (specific keys):**
```json
{
  "key_ids": [123, 124, 125]
}
```

**Request (all keys):**
```json
{}
```
With query parameter: `?all=true`

**Response:**
```json
{
  "checked": 3,
  "results": [
    {
      "key_id": 123,
      "status": "active",
      "error_class": null,
      "error_detail": null,
      "response_time_ms": 234.5,
      "checked_at": 1717654321.0
    }
  ]
}
```

---

### DELETE `/keys/{key_id}`

Delete a key from monitoring.

**Authentication:** Required

**Response:**
```json
{
  "ok": true,
  "deleted_id": 123
}
```

---

### POST `/check-duplicate`

Check if keys already exist (by hash).

**Authentication:** Not required (read-only)

**Request:**
```json
{
  "keys": ["AIzaSyABC...", "sk-proj-XYZ..."]
}
```

**Response:**
```json
{
  "duplicates": [
    {
      "key_hash": "sha256:abc123...",
      "exists": true,
      "key_id": 123
    },
    {
      "key_hash": "sha256:def456...",
      "exists": false,
      "key_id": null
    }
  ]
}
```

---

## Status Values

- `active`: Key is working
- `dead`: Key failed and will be retried less frequently
- `abandoned`: Key has been dead too long and is no longer checked

---

## Platform Values

- `gemini`: Google Gemini API
- `openai`: OpenAI API
- `anthropic`: Anthropic Claude API
- `gcp`: Google Cloud Platform (Vertex AI)

---

## Frontend Integration Example

```javascript
// Login
async function login(password) {
  const res = await fetch('/api/long-term/auth', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password })
  });
  const { token } = await res.json();
  localStorage.setItem('adminToken', token);
  return token;
}

// List keys
async function listKeys(filters = {}) {
  const token = localStorage.getItem('adminToken');
  const params = new URLSearchParams(filters);
  const res = await fetch(`/api/long-term/keys?${params}`, {
    headers: { 'Authorization': `Bearer ${token}` }
  });
  return res.json();
}

// Check single key
async function checkKey(keyId) {
  const token = localStorage.getItem('adminToken');
  const res = await fetch(`/api/long-term/keys/${keyId}/check`, {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${token}` }
  });
  return res.json();
}

// Delete key
async function deleteKey(keyId) {
  const token = localStorage.getItem('adminToken');
  const res = await fetch(`/api/long-term/keys/${keyId}`, {
    method: 'DELETE',
    headers: { 'Authorization': `Bearer ${token}` }
  });
  return res.json();
}

// Move keys from short-term
async function moveKeys(keys, platform, notes) {
  const token = localStorage.getItem('adminToken');
  const res = await fetch('/api/long-term/keys/move', {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${token}`,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({ keys, platform, notes })
  });
  return res.json();
}
```

---

## Access URLs

- **Main Checker:** `http://localhost:8000/`
- **Admin Panel:** `http://localhost:8000/admin`
- **API Docs:** `http://localhost:8000/docs`

---

## Security Notes

1. **Production:** Change `ADMIN_PASSWORD` and `JWT_SECRET` via environment variables
2. **HTTPS:** Always use HTTPS in production
3. **Token Storage:** Frontend stores JWT in `localStorage` (24-hour expiry)
4. **Rate Limiting:** Consider adding rate limiting for auth endpoint
5. **CORS:** Configure CORS if frontend is on different domain
