// Next.js 16 renamed Middleware to Proxy. Gates the operator console.
// All non-login routes require the `console_session` cookie set by
// insights-api (fastapi-users). The cookie is HttpOnly; we can only check
// presence, not contents — the backend verifies the JWT on every API call.

import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

const PUBLIC_PATHS = new Set(["/login"]);
// insights-api's cookie name (set in apps/insights-api/insights_api/auth/backends.py).
// IMPORTANT: distinct from chat-service's `chat_session` so the two surfaces
// have independent sessions even when they share a domain.
const COOKIE_NAME = "console_session";

export function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;

  if (PUBLIC_PATHS.has(pathname)) {
    return NextResponse.next();
  }

  const hasSession = request.cookies.has(COOKIE_NAME);
  if (!hasSession) {
    const loginUrl = new URL("/login", request.url);
    if (pathname !== "/") {
      loginUrl.searchParams.set("next", pathname);
    }
    return NextResponse.redirect(loginUrl);
  }

  return NextResponse.next();
}

export const config = {
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp)$).*)",
  ],
};
