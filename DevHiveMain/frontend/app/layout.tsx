import type { Metadata } from "next";
import "./globals.css";
import "./insight.css";
import "./ask.css";

export const metadata: Metadata = {
  title: "DevHive",
  description: "Projects, source history and engineering memory",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
