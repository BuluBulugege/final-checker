# Long-Term Key Management Admin Panel

## Overview

The admin panel provides a web interface for managing API keys in long-term monitoring storage. Access it at `http://localhost:8000/admin` after starting the server.

## Features

### 1. Authentication
- **Password-based login** with JWT token (24-hour expiry)
- Default password: `bingxujingAb` (change in production via environment variable)
- Token stored in browser localStorage

### 2. Key List & Filtering
- **View all monitored keys** with masked display
- **Filter by:**
  - Platform (Gemini, OpenAI, Anthropic, GCP)
  - Status (Active, Dead, Abandoned)
  - Search term (notes or key fragment)
- **Pagination** (50 keys per page)
- **Real-time status display:**
  - 🟢 Active (green badge)
  - 🔴 Dead (red badge)
  - ⚫ Abandoned (gray badge)

### 3. Batch Operations
- **Select All / Deselect All** - Manage multiple keys at once
- **Batch Check** - Health check selected keys
- **Batch Delete** - Remove selected keys (with confirmation)
- **Check All** - Health check all active and dead keys

### 4. Single Key Actions
Each key row has action buttons:
- **探活 (Check)** - Manually trigger health check
- **删除 (Delete)** - Remove key from monitoring

### 5. Move Keys from Short-Term
- **Import keys** from one-time checker results into long-term monitoring
- Paste multiple keys (one per line)
- Select platform
- Add optional notes
- Automatic duplicate detection

## Key Information Display

Each key shows:
- **ID** - Database identifier
- **Platform** - Gemini / OpenAI / Anthropic / GCP
- **Masked Key** - First 10 and last 5 characters
- **Status** - Active / Dead / Abandoned
- **Last Check** - Relative time (e.g., "2小时前")
- **Error Code** - Error classification if dead
- **Next Check** - Scheduled next check time
- **Notes** - Custom description

## Status Meanings

| Status | Description | Check Frequency |
|--------|-------------|-----------------|
| **Active** | Key is working | Every 6-24 hours (adaptive) |
| **Dead** | Key failed, being retried | Every 6 hours (with exponential backoff) |
| **Abandoned** | Dead for >30 days | No longer checked |

## Workflow Example

### Scenario: Monitor production keys

1. **Login** to admin panel
2. **Move keys** from short-term checker:
   - Run batch check in main UI
   - Copy alive keys
   - Go to admin panel → "短期库迁移"
   - Paste keys, select platform, add note "Production keys"
   - Click "移入监控"
3. **Filter** by platform to view specific keys
4. **Check All** to verify all keys immediately
5. **Monitor status** - keys will be checked automatically by scheduler
6. **Delete dead keys** when no longer needed

## Auto-Refresh

The admin panel does NOT auto-refresh by default to avoid disrupting user actions. Click the **⟳ 刷新** button to manually reload the key list.

### Optional: Add auto-refresh
To enable automatic updates, add this to `admin.js`:

```javascript
// Add to init() function
setInterval(() => {
  if (document.visibilityState === 'visible') {
    loadKeys();
  }
}, 30000); // Refresh every 30 seconds when tab is active
```

## Design System

Follows the **Neo-Brutalism** design language:
- **Colors:**
  - Cream background (`#FFFEF5`)
  - Orange primary (`#FF6B35`)
  - Blue secondary (`#004E98`)
  - Green success (`#2D9B4E`)
  - Yellow accent (`#FFD23F`)
  - Red danger (`#E23A3A`)
  - Ink borders (`#1A1423`)
- **Typography:** Sora + Noto Sans SC
- **UI Elements:**
  - Bold 2.5px borders
  - Hard 4px shadows
  - High contrast badges
  - Inline styles (no CSS modules)

## Security Considerations

### Production Deployment

1. **Change admin password:**
   ```bash
   export ADMIN_PASSWORD="your-secure-password-here"
   ```

2. **Change JWT secret:**
   ```bash
   export JWT_SECRET="your-secret-key-here"
   ```

3. **Use HTTPS** - Never expose admin panel over HTTP in production

4. **Add rate limiting** for auth endpoint:
   ```python
   from slowapi import Limiter
   limiter = Limiter(key_func=get_remote_address)
   
   @router.post("/auth")
   @limiter.limit("5/minute")
   async def admin_auth(req: AuthRequest):
       ...
   ```

5. **Consider IP allowlist** for admin routes

6. **Enable audit logging** for all admin actions

## Troubleshooting

### "认证失败，请重新登录"
- Token expired (24h limit) - login again
- Check browser console for 401 errors
- Clear localStorage and re-login

### Keys not loading
- Check server logs for errors
- Verify database file exists: `data.db`
- Check API endpoint: `GET /api/long-term/keys`

### Batch operations slow
- Large batch checks may take time (each key needs API call)
- Consider smaller batches or use "Check All" during off-peak hours
- Monitor server logs for rate limit warnings

### Pagination issues
- Database query may be slow with many keys
- Consider adding database indexes if >10k keys
- Adjust `pageSize` in `admin.js` if needed

## Development

### File Structure
```
app/static/
├── admin.html      # Admin panel HTML
├── admin.css       # Neo-Brutalism styles
├── admin.js        # Frontend controller
├── index.html      # Main checker UI
├── app.css         # Main styles
└── app.js          # Main controller

app/
├── api_long_term.py    # Admin API routes
├── long_term_monitor.py # Key management logic
├── scheduler.py        # Background check scheduler
└── main.py            # FastAPI app + admin route
```

### Testing API Endpoints

```bash
# Login
curl -X POST http://localhost:8000/api/long-term/auth \
  -H "Content-Type: application/json" \
  -d '{"password":"bingxujingAb"}'

# List keys (with token)
curl http://localhost:8000/api/long-term/keys \
  -H "Authorization: Bearer YOUR_TOKEN_HERE"

# Check single key
curl -X POST http://localhost:8000/api/long-term/keys/123/check \
  -H "Authorization: Bearer YOUR_TOKEN_HERE"
```

## Future Enhancements

Potential improvements:
- [ ] Export keys to CSV/JSON
- [ ] Import keys from CSV
- [ ] Bulk edit notes
- [ ] Key usage statistics dashboard
- [ ] Email/webhook alerts for dead keys
- [ ] Multi-user support with roles
- [ ] Audit log viewer
- [ ] Advanced search (date ranges, error codes)
- [ ] Key tagging system
- [ ] Auto-archive old abandoned keys

## Support

For API documentation, see [LONG_TERM_API.md](../LONG_TERM_API.md)
