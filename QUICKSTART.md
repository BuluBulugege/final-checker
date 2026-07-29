# Quick Start: Long-Term Monitor Admin Panel

## Launch in 3 Steps

### 1. Start the Server
```bash
cd /Users/bluedog/develop/final-checker
uvicorn app.main:app --reload
```

### 2. Open Admin Panel
Navigate to: **http://localhost:8000/admin**

### 3. Login
Password: `change-me-in-production`

---

## First-Time Setup

### Import Keys from Main Checker

1. Go to main checker: http://localhost:8000/
2. Paste your API keys and run check
3. Copy the alive keys
4. Go to admin panel: http://localhost:8000/admin
5. Navigate to sidebar → **"短期库迁移"**
6. Paste keys (one per line)
7. Select platform (gemini/openai/anthropic/gcp)
8. Add notes (optional): "Production batch 1"
9. Click **"移入监控"**

✅ Keys are now monitored automatically!

---

## Daily Operations

### View All Keys
- Login → Main table shows all monitored keys
- Status colors: 🟢 Active | 🔴 Dead | ⚫ Abandoned

### Filter Keys
Left sidebar → Set filters:
- Platform dropdown
- Status dropdown
- Search box
- Click **"应用筛选"**

### Check Keys Manually
**Single key:** Click **"探活"** button in table row

**Multiple keys:**
1. Select checkboxes
2. Click **"探活选中"**

**All keys:** Click **"探活全部"** (checks all active+dead keys)

### Delete Keys
**Single key:** Click **"删除"** button → Confirm

**Multiple keys:**
1. Select checkboxes
2. Click **"删除选中"** → Confirm

---

## Background Monitoring

Keys are checked automatically by the scheduler:
- **Active keys:** Every 6-24 hours (adaptive)
- **Dead keys:** Every 6 hours (with backoff)
- **Abandoned keys:** Not checked (dead >30 days)

No manual action required! The system keeps keys up-to-date.

---

## Troubleshooting

### Can't login?
- Check password: `change-me-in-production` (default)
- Server running? Check terminal for errors
- Try clearing browser localStorage

### Keys not showing?
- Click **⟳ 刷新** button
- Check filters (clear with **"清除"**)
- Verify database exists: `data.db`

### Token expired?
- JWT lasts 24 hours
- Logout and login again
- Error shows: "认证失败，请重新登录"

---

## Production Checklist

Before deploying:

- [ ] Change admin password:
  ```bash
  export ADMIN_PASSWORD="your-secure-password"
  ```

- [ ] Change JWT secret:
  ```bash
  export JWT_SECRET="your-secret-32-char-minimum"
  ```

- [ ] Enable HTTPS (never HTTP in production)

- [ ] Add rate limiting to `/auth` endpoint

- [ ] Configure firewall/IP allowlist

- [ ] Set up backup for `data.db`

---

## Architecture

```
┌─────────────────────────────────────────┐
│  Browser                                │
│  - admin.html (UI)                      │
│  - admin.js (controller)                │
│  - admin.css (styles)                   │
│  - localStorage (JWT token)             │
└──────────────┬──────────────────────────┘
               │
               │ HTTP + Bearer Token
               │
┌──────────────▼──────────────────────────┐
│  FastAPI Server (main.py)               │
│  GET /admin → admin.html                │
│  /api/long-term/* → api_long_term.py    │
└──────────────┬──────────────────────────┘
               │
               │ SQL queries
               │
┌──────────────▼──────────────────────────┐
│  SQLite Database (data.db)              │
│  - long_term_keys table                 │
│  - long_term_check_history table        │
└──────────────┬──────────────────────────┘
               │
               │ Background tasks
               │
┌──────────────▼──────────────────────────┐
│  Scheduler (scheduler.py)               │
│  - APScheduler                          │
│  - Periodic health checks               │
│  - Adaptive retry logic                 │
└─────────────────────────────────────────┘
```

---

## Key Files Reference

| File | Purpose |
|------|---------|
| `app/static/admin.html` | Admin UI structure |
| `app/static/admin.css` | Neo-brutalism styles |
| `app/static/admin.js` | Frontend logic |
| `app/api_long_term.py` | REST API endpoints |
| `app/long_term_monitor.py` | Key management logic |
| `app/scheduler.py` | Background checker |
| `app/main.py` | FastAPI app + routes |
| `data.db` | SQLite database |

---

## Documentation

- **API Reference:** [LONG_TERM_API.md](LONG_TERM_API.md)
- **User Guide:** [ADMIN_PANEL.md](ADMIN_PANEL.md)
- **Implementation:** [IMPLEMENTATION_SUMMARY.md](IMPLEMENTATION_SUMMARY.md)

---

## Support

**Default credentials:**
- Password: `change-me-in-production`
- JWT expiry: 24 hours

**Endpoints:**
- Main UI: http://localhost:8000/
- Admin Panel: http://localhost:8000/admin
- API Docs: http://localhost:8000/docs

---

**Ready to monitor!** 🚀
