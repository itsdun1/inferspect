import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Ovel Assistant",
  description: "LLM observability platform",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full flex flex-col bg-gray-50 text-gray-900">
        {children}
      </body>
    </html>
  );
}
