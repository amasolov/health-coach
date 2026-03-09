# iFit API Traffic Analysis Report

**Source:** 500 captured HTTP requests from `.ifit_capture/`  
**Date:** 2026-03-08  
**Purpose:** Build an iFit API client

---

## 1. Auth Flow

### 1.1 Overview

iFit uses OAuth2-style tokens with two auth methods:

| Method | Endpoint | Use Case |
|--------|----------|----------|
| **Password login** | `POST /cockatoo/v3/user/login` | Initial login (web/pigeon.ifit.com) |
| **Refresh token** | `POST /cockatoo/v2/login/refresh` | Token refresh (app/GLSUSRAUTH) |

### 1.2 Password Login (cockatoo v3)

**URL:** `https://gateway.ifit.com/cockatoo/v3/user/login`

**Request:**
- **Method:** POST
- **Headers:**
  - `Content-Type: application/json`
  - `Authorization: Basic <client_credentials>` (Base64-encoded client_id:client_secret)
  - `x-aws-waf-token` (required for web login – AWS WAF challenge token)
  - `Origin: https://pigeon.ifit.com`
  - `Referer: https://pigeon.ifit.com/`

**Request body:**
```json
{
  "username": "<email>",
  "password": "<password>"
}
```

**Response (200):**
```json
{
  "access_token": "<JWT>",
  "refresh_token": "v1.<opaque_string>",
  "token_type": "Bearer",
  "expires_in": 604800
}
```

- `expires_in`: 604800 seconds = 7 days
- Web login uses different Basic credentials than app refresh

### 1.3 Token Refresh (cockatoo v2)

**URL:** `https://gateway.ifit.com/cockatoo/v2/login/refresh`

**Request:**
- **Method:** POST
- **Headers:**
  - `Content-Type: application/json`
  - `Authorization: Basic <client_credentials>` (app client credentials)
  - `User-Agent: GLSUSRAUTH v10.1.3` (iFit app auth library)

**Request body:**
```json
{
  "refresh_token": "v1.<opaque_string>"
}
```

**Response (200):** Same structure as password login.

### 1.4 Client Credentials (Basic Auth)

Two distinct Basic credentials are used:

| Context | Purpose |
|---------|---------|
| App (GLSUSRAUTH) | OAuth2 client credentials for token refresh |
| Web (pigeon.ifit.com) | OAuth2 client credentials for password login |

**Format:** `Authorization: Basic <base64(client_id:client_secret)>`

**Note:** Extract client_id and client_secret from the captured traffic. These are OAuth2 app credentials and may be app-specific.

### 1.5 JWT Access Token (decoded payload)

The access token is a JWT (RS256) with claims including:

- `ifit_userId`: MongoDB ObjectId (e.g. `647878ae1e36270040d6413a`)
- `ifit_acl`: Array of permissions (user, premium, coach-plus, etc.)
- `ifit_isGuest`: boolean
- `sub`: Auth0-style subject (`auth0|<userId>`)
- `aud`: `["ifit-api", "https://ifit-prod.auth0.com/userinfo"]`
- `exp`: Unix timestamp (7 days from issue)
- `scope`: `openid profile email address phone offline_access`

---

## 2. API Map (by Service)

Endpoints use two base domains:
- `https://gateway.ifit.com` – primary API gateway
- `https://gateway-cache.ifit.com` – cached endpoints
- `https://api.ifit.com` – legacy/user API
- `https://sms-service.svc.ifit.com` – SMS/opt-in
- `https://membership-svc.svc.ifit.com` – membership

### 2.1 cockatoo (Auth & User)

| Method | URL Pattern | Auth |
|--------|-------------|------|
| POST | `/cockatoo/v2/login/refresh` | Basic |
| POST | `/cockatoo/v3/user/login` | Basic |
| GET | `/cockatoo/v1/user` | Bearer |
| GET | `/cockatoo/v2/legal-receipts` | Bearer |

### 2.2 api.ifit.com (Legacy User API)

| Method | URL Pattern | Auth |
|--------|-------------|------|
| GET | `/v1/me` | Bearer |
| GET | `/v1/users/{userId}` | Bearer |
| GET | `/v1/users/{userId}/image?size=large` | Bearer |

### 2.3 wolf-dashboard-service

| Method | URL Pattern | Auth |
|--------|-------------|------|
| GET | `/wolf-dashboard-service/v1/up-next?softwareNumber={id}&limit={n}&challengeStoreEnabled={bool}&userType={type}` | Bearer |
| GET | `/wolf-dashboard-service/v1/user-workout-details?softwareNumber={id}&workoutIds[]={id}&isClub={bool}&isEmbedded={bool}` | Bearer |
| GET | `/wolf-dashboard-service/v1/user-series-details?softwareNumber={id}&seriesIds[]={id}&isClub={bool}` | Bearer |
| GET | `/wolf-dashboard-service/v2/user_stats/aggregate?before={date}&after={date}&equipmentType={type}` | Bearer |
| GET | `/wolf-dashboard-service/v1/recommended-workouts?softwareNumber={id}&limit={n}` | Bearer |
| GET | `/wolf-dashboard-service/v1/recommended-series?softwareNumber={id}&limit={n}` | Bearer |
| GET | `/wolf-dashboard-service/v1/favorites?challengeStoreEnabled={bool}&softwareNumber={id}&page={n}&pageSize={n}` | Bearer |
| GET | `/wolf-dashboard-service/v1/live-workouts?softwareNumber={id}&page={n}&pageSize={n}` | Bearer |
| GET | `/wolf-dashboard-service/v1/matrix-club?softwareNumber={id}&equipmentType={type}&startingDate={date}&userType={type}&limit={n}` | Basic or Bearer |
| GET | `/wolf-dashboard-service/v1/staff-picks?softwareNumber={id}&startingDate={date}&userType={type}` | Bearer |
| GET | `/wolf-dashboard-service/v1/quick-finds?softwareNumber={id}&limit={n}` | Bearer |
| GET | `/wolf-dashboard-service/v2/dashboard/unlocked-workout/{softwareNumber}?equipmentType={type}&userType={type}` | Bearer |
| GET | `/wolf-dashboard-service/v2/cacheable/dashboard/equipment/{softwareNumber}?equipmentType={type}&platform={platform}&date={date}&userType={type}&isClub={bool}&isEmbedded={bool}&doEnableScaling={bool}` | Basic or Bearer |

### 2.4 wolf-athenaeum-service (Library & Challenges)

| Method | URL Pattern | Auth |
|--------|-------------|------|
| GET | `/wolf-athenaeum-service/v2/user-library/challenges?softwareNumber={id}&membershipType={type}&modality={type}` | Bearer |

### 2.5 wolf-workouts-service

| Method | URL Pattern | Auth |
|--------|-------------|------|
| GET | `/wolf-workouts-service/v1/get-wotd?softwareNumber={id}&cb={version}` | Bearer |
| GET | `/wolf-workouts-service/v1/user-avatar?userId={id}&cb={version}` | Bearer |
| GET | `/wolf-workouts-service/v1/workout/{workoutId}?softwareNumber={id}&langCode={code}&cb={version}` | Bearer |
| GET | `/wolf-workouts-service/v1/workout/{workoutId}/comment-card?cb={version}` | Bearer |

### 2.6 lumberjack (Activity Logs)

| Method | URL Pattern | Auth |
|--------|-------------|------|
| GET | `/lumberjack/v1/activity-logs/milestones?year={year}&userId={userId}` | Bearer |

### 2.7 achievement-service

| Method | URL Pattern | Auth |
|--------|-------------|------|
| GET | `/achievement-service/v1/achievements?type=milestone&page={n}&perPage={n}&milestoneYear={year}` | Bearer |

### 2.8 pulse (Heart Rate)

| Method | URL Pattern | Auth |
|--------|-------------|------|
| GET | `/pulse/v1/heart-rate` | Bearer |

### 2.9 lycan (Workout Metadata)

| Method | URL Pattern | Auth |
|--------|-------------|------|
| GET | `/lycan/v1/workouts/{workoutId}` | Bearer |
| GET | `/lycan/v1/workouts/{workoutId}?softwareNumber={id}&doEnableScaling={bool}` | Bearer |

### 2.10 ceol (Workout Music)

| Method | URL Pattern | Auth |
|--------|-------------|------|
| GET | `/ceol/v2/workout-music/{workoutId}` | Bearer |

### 2.11 video-streaming-service

| Method | URL Pattern | Auth |
|--------|-------------|------|
| GET | `/video-streaming-service/v1/workoutVideo/{workoutId}?useFullManifest={bool}` | Bearer |

### 2.12 console-store

| Method | URL Pattern | Auth |
|--------|-------------|------|
| GET | `/console-store/v1/stationary-console?limit={n}&softwareNumber={id}` | Bearer |

### 2.13 Other Services

| Method | URL Pattern | Auth |
|--------|-------------|------|
| GET | `sms-service.svc.ifit.com/v1/user/opt-in-status` | Bearer |
| GET | `membership-svc.svc.ifit.com/membership/{userId}` | Bearer |

---

## 3. Key Data Structures

### 3.1 User Profile (`/v1/me`, `cockatoo/v1/user`)

```typescript
interface UserProfile {
  id: string;                    // MongoDB ObjectId
  username: string;
  firstname: string;
  lastname: string;
  email: string;
  gender: string;
  country: string;
  birthday: string;              // "YYYY-MM-DD"
  height: number;                // meters
  weight: number;                // kg
  premium: boolean;
  subscription_type: string;
  activity_level: string;
  workout_days: string[];
  equipment: string[];
  acl: string[];                 // permissions
  unit_system: "metric" | "imperial";
  timezone: string;
  locale: string;
  // ... many more fields
}
```

### 3.2 Activity Logs Milestones (`lumberjack/v1/activity-logs/milestones`)

```typescript
interface ActivityMilestones {
  countYTD: number;   // workouts year-to-date
  count: number;
}
```

### 3.3 User Stats Aggregate (`wolf-dashboard-service/v2/user_stats/aggregate`)

```typescript
interface UserStatsAggregate {
  stats: Array<{
    formatted: string;
    label: string;
    type: "duration" | "distance" | "cals" | "elevation-gain" | "pace" | "avg-watts" | "workouts-count";
    value: string;
    unit: string;
  }>;
}
```

### 3.4 Recommended Workouts (`wolf-dashboard-service/v1/recommended-workouts`)

```typescript
interface RecommendedWorkout {
  id: string;
  type: "recommended-workouts";
  title: string;
  description: string;
  imageUrl: string;
  navigationUrl: string;
  isLive: boolean;
  leadLine: string;
  callToAction: string;
  averageRating: string;
  intensity: number;
  intensityText: string;
  workoutLabelsCount: Array<{ count: number; type: string; label: string }>;
  source: string;
}
```

### 3.5 User Workout Details (`wolf-dashboard-service/v1/user-workout-details`)

```typescript
interface UserWorkoutDetail {
  workoutId: string;
  completedDate: string | null;   // ISO date or null
  locked: boolean;
  estimates: {
    calories: string;
    duration: string;
    distance: string | null;
  };
}
```

### 3.6 User Library Challenges (`wolf-athenaeum-service/v2/user-library/challenges`)

```typescript
interface UserLibraryChallenges {
  data: Array<{
    title: string;
    data: Array<{
      type: "challenge";
      item: {
        itemId: string;
        image: string;
        title: string;
        description: string;
        rating: { average: number | null; user_weighted: number | null };
        challengeEndTimestamp: number;
        workouts: string[];
        intensity: number | null;
      };
    }>;
  }>;
}
```

### 3.7 Up Next (`wolf-dashboard-service/v1/up-next`)

```typescript
interface UpNextItem {
  type: "continue-series-workout" | string;
  title: string;
  subtitle: string;
  leadLine: string;
  callToAction: string;
  callToActionShort: string;
  tagPrimary: string;
  imageUrl: string;
  navigationUrl: string;
  isLive: boolean;
  progress: number;
  workoutId: string;
  seriesId: string;
  isSeriesCompleted: boolean;
  source: string;
}
```

### 3.8 Achievements (`achievement-service/v1/achievements`)

```typescript
interface Achievement {
  id: number;
  title: string;
  description: string;
  badgeImage: string;
  largeBadgeImage: string | null;
  type: "milestone";
  milestoneCount: number;
  milestoneYear: number;
  hasTiers: boolean;
  isFinalized: boolean;
  updatedAt: string;
  createdAt: string;
}
```

### 3.9 Heart Rate (`pulse/v1/heart-rate`)

```typescript
interface HeartRateProfile {
  createdAt: string;
  updatedAt: string;
  maxHeartRate: number;
  restingHeartRate: number;
  heartRateReserve: number;
  heartRateZones: number[][];   // [[min, max], ...] for each zone
}
```

### 3.10 Cacheable Dashboard (`wolf-dashboard-service/v2/cacheable/dashboard/equipment/{id}`)

```typescript
interface DashboardItem {
  locked: boolean;
  id: string;
  cardSize: string;
  workout: {
    id: string;
    currentUserWorkoutRating: number;
    workoutRating: number;
    timesRated: number;
    trainer: { id: string; name: string; image: string };
    estimates: unknown[];
  };
  images: { coverImage: string; thumbnail: string; largeImage: string };
  labels: Array<{ type: string; text: string }>;
  type: string;
  title: string;
}
```

---

## 4. User ID

**iFit User ID:** `647878ae1e36270040d6413a`

Found in:
- `/v1/me` response `id`
- JWT claim `ifit_userId`
- JWT claim `sub` (as `auth0|647878ae1e36270040d6413a`)
- All user-scoped API URLs (`userId=647878ae1e36270040d6413a`)

---

## 5. Authorization Header

### 5.1 Bearer Token (Authenticated API Calls)

**Format:** `Authorization: Bearer <access_token>`

The access token is a JWT (RS256) with three parts (header.payload.signature).

**Example header (redacted):**
```
Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCIsImtpZCI6Ilk5UVZHZGF0QlVwOGNxd1k5VjJMLSJ9.<payload>.<signature>
```

**Required for:** All gateway/API calls except auth endpoints (login, refresh).

### 5.2 Basic Auth (Auth Endpoints)

**Format:** `Authorization: Basic <base64(client_id:client_secret)>`

Used for:
- `POST /cockatoo/v2/login/refresh`
- `POST /cockatoo/v3/user/login`

### 5.3 Optional Headers

- `Accept: application/json`
- `Content-Type: application/json` (for POST)
- `Accept-Language: en-AU,en;q=0.9`
- `language: en` (some wolf-dashboard endpoints)

### 5.4 Special Cases

- **gateway-cache.ifit.com** – Some endpoints (e.g. `cacheable/dashboard/equipment`, `matrix-club`) use **Basic** auth instead of Bearer (different credentials for anonymous/cached content).
- **x-aws-waf-token** – Required for web login from pigeon.ifit.com (AWS WAF challenge).

---

## 6. Implementation Notes for API Client

1. **Token lifecycle:** Access tokens expire in 7 days. Use refresh token before expiry.
2. **Base URLs:** Prefer `gateway.ifit.com`; use `gateway-cache.ifit.com` where caching is beneficial.
3. **softwareNumber:** Many endpoints require `softwareNumber` (e.g. `424992`, `424110`) – appears to be equipment/app instance ID.
4. **userType:** Often `premium` or similar for content filtering.
5. **equipmentType:** `strength`, `run`, `treadmill`, `all`, etc.
6. **modality:** `strength`, `treadmill` for library/challenges.
7. **OPTIONS:** Skip CORS preflight (OPTIONS) requests; they don’t return data.

---

## Appendix: Path Parameter Placeholders

| Placeholder | Description | Example |
|-------------|-------------|---------|
| `{userId}` | User MongoDB ObjectId | `647878ae1e36270040d6413a` |
| `{workoutId}` | Workout ID | `693af1e937128c00088e14b8` |
| `{seriesId}` | Series/program ID | `694ae0bc2dea2428143deeea` |
| `{softwareNumber}` | Equipment/app instance | `424992`, `424110` |
| `{date}` | ISO date | `2026-03-08` |
| `{year}` | Year | `2026` |
| `{n}` | Numeric (page, limit, etc.) | `1`, `10` |
| `{type}` | Type string | `milestone`, `strength`, `premium` |
| `{bool}` | Boolean | `true`, `false` |
