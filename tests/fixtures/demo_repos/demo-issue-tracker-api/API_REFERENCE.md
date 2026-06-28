# API Reference - Demo Issue Tracker API

Complete reference for all endpoints in the Demo Issue Tracker API.

## Base URL

```
http://localhost:8000
```

---

## Endpoints

### Health Check

#### `GET /health`

Simple health check endpoint to verify the API is running.

**Response:**
```json
{
  "status": "ok"
}
```

**Status Code:** `200 OK`

**Use Case:** Monitoring, load balancer health checks, readiness probes.

---

### Hello

#### `GET /hello`

Returns a simple greeting message.

**Response:**
```json
{
  "message": "Hello, World!"
}
```

**Status Code:** `200 OK`

**Use Case:** Demo/test endpoint to verify basic API functionality.

---

### Joke

#### `GET /joke`

Returns a programming-related joke.

**Response:**
```json
{
  "joke": "Why did the API go to the bar? To get a REST!"
}
```

**Status Code:** `200 OK`

**Use Case:** Fun demo endpoint, example of custom endpoints in the API.

---

## Ticket Endpoints

### List Tickets

#### `GET /tickets`

Retrieve a list of tickets with optional filtering and search.

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `include_archived` | boolean | `false` | Include archived tickets in the response |
| `search` | string | `null` | Search query (matches title and description, case-insensitive) |

**Response:**
```json
{
  "items": [
    {
      "id": 1,
      "title": "Login form not responding",
      "description": "The login button doesn't work on mobile devices",
      "customer_email": "john@example.com",
      "priority": "high",
      "status": "open",
      "archived": false,
      "created_at": "2026-03-26T10:30:00Z"
    }
  ],
  "total": 1
}
```

**Status Code:** `200 OK`

**Example Requests:**

```bash
# List all active tickets
curl http://localhost:8000/tickets

# List all tickets including archived
curl http://localhost:8000/tickets?include_archived=true

# Search for tickets
curl http://localhost:8000/tickets?search=login

# Search with archived inclusion
curl http://localhost:8000/tickets?search=bug&include_archived=true
```

---

### Create Ticket

#### `POST /tickets`

Create a new ticket.

**Request Body:**
```json
{
  "title": "string (required)",
  "description": "string (required)",
  "customer_email": "string (required)",
  "priority": "low|medium|high (required)"
}
```

**Response:** Returns the created ticket with auto-generated ID and timestamp.
```json
{
  "id": 5,
  "title": "Broken mobile nav",
  "description": "Nav drawer overlays the footer on narrow screens.",
  "customer_email": "ops@example.com",
  "priority": "high",
  "status": "open",
  "archived": false,
  "created_at": "2026-04-12T14:22:00Z"
}
```

**Status Code:** `201 Created`

**Example Request:**

```bash
curl -X POST http://localhost:8000/tickets \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Broken mobile nav",
    "description": "Nav drawer overlays the footer on narrow screens.",
    "customer_email": "ops@example.com",
    "priority": "high"
  }'
```

**Notes:**
- Title and description are trimmed of whitespace
- Email is trimmed of whitespace (no validation of format)
- Status defaults to "open"
- Created timestamp is automatically set to current UTC time

---

### Archive Ticket

#### `PATCH /tickets/{ticket_id}/archive`

Archive an existing ticket (mark as archived without deletion).

**Path Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `ticket_id` | integer | ID of the ticket to archive |

**Response:** Returns the archived ticket with `archived` flag set to `true`.
```json
{
  "id": 3,
  "title": "Webhook integration failing",
  "description": "POST requests to webhook endpoint are timing out",
  "customer_email": "dev@example.com",
  "priority": "high",
  "status": "resolved",
  "archived": true,
  "created_at": "2026-03-28T09:15:00Z"
}
```

**Status Code:** `200 OK`

**Error Response (Ticket Not Found):**
```json
{
  "detail": "Ticket not found"
}
```

**Status Code:** `404 Not Found`

**Example Request:**

```bash
curl -X PATCH http://localhost:8000/tickets/3/archive
```

---

## Data Models

### Ticket Priority

An enumeration with the following values:
- `low`
- `medium`
- `high`

### Ticket Status

An enumeration with the following values:
- `open`
- `investigating`
- `resolved`
- `archived`

### Ticket Object

| Field | Type | Description |
|-------|------|-------------|
| `id` | integer | Auto-generated ticket identifier |
| `title` | string | Ticket subject line |
| `description` | string | Detailed description of the issue |
| `customer_email` | string | Email address of the reporting customer |
| `priority` | string | Priority level (low, medium, high) |
| `status` | string | Current status (open, investigating, resolved, archived) |
| `archived` | boolean | Whether the ticket is archived |
| `created_at` | string (ISO 8601) | Timestamp of ticket creation in UTC |

---

## Error Responses

### 404 Not Found

Returned when attempting to access a non-existent resource (e.g., archiving a ticket that doesn't exist).

```json
{
  "detail": "Ticket not found"
}
```

### 400 Bad Request

Returned when request validation fails (e.g., invalid payload format).

---

## Testing

All endpoints are covered by integration tests. Run tests with:

```bash
uv run pytest
```

For specific endpoint testing:

```bash
uv run pytest -v -k "test_healthcheck"
uv run pytest -v -k "test_hello"
uv run pytest -v -k "test_joke"
uv run pytest -v -k "test_create_ticket"
uv run pytest -v -k "test_list_tickets"
uv run pytest -v -k "test_archive"
```
