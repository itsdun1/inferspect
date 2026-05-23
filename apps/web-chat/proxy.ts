// Next.js 16 renamed Middleware to Proxy. This file gates protected routes
// by checking for the chat_session cookie set by chat-service (fastapi-users).
// It does NOT verify the JWT — that happens server-side on every API call.
// This is an "optimistic check" per the Next docs, suitable for UX-level
// routing.

import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

const PUBLIC_PATHS = new Set(["/login", "/register"]);
// IMPORTANT: this is chat-service's cookie (set by apps/chat-service's
// CookieTransport). web-insights uses `console_session` instead — the two
// surfaces issue independent sessions.
const COOKIE_NAME = "chat_session";

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
    /*
     * Match everything except:
     *  - static assets (_next/static, _next/image)
     *  - favicon
     *  - login + register (handled by PUBLIC_PATHS above)
     */
    "/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp)$).*)",
  ],
};
