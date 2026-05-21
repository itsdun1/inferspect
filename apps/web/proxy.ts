// Next.js 16 renamed Middleware to Proxy. This file gates protected routes
// by checking for the auth cookie set by fastapi-users. It does NOT verify
// the JWT — that happens server-side on every API call. This is an
// "optimistic check" per the Next docs, suitable for UX-level routing.

import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

const PUBLIC_PATHS = new Set(["/login", "/register"]);
const COOKIE_NAME = "ollive_session";

export function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;

  // Static / public paths bypass the check.
  if (PUBLIC_PATHS.has(pathname)) {
    return NextResponse.next();
  }

  // The fastapi-users cookie is HttpOnly so we can only check for its
  // presence, not its contents. Server-side endpoints verify the JWT.
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
     *  - api routes (we don't have any in Next; the backend is separate)
     *  - static assets (_next/static, _next/image)
     *  - favicon
     *  - the login + register pages (handled by PUBLIC_PATHS above)
     */
    "/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp)$).*)",
  ],
};
