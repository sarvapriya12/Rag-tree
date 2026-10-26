import "server-only";
import { createCipheriv, createDecipheriv, createHash, randomBytes } from "node:crypto";
import { cookies } from "next/headers";
import { prisma } from "@/lib/prisma";

const COOKIE_NAME = "devhive_session";

function encryptionKey(): Buffer {
  const secret = process.env.NEXT_SESSION_SECRET ?? "";
  if (secret.length < 32) throw new Error("NEXT_SESSION_SECRET must be at least 32 characters");
  return createHash("sha256").update(secret).digest();
}

function hash(value: string) {
  return createHash("sha256").update(value).digest("hex");
}

function encrypt(value: string): string {
  const iv = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", encryptionKey(), iv);
  const data = Buffer.concat([cipher.update(value, "utf8"), cipher.final()]);
  return Buffer.concat([iv, cipher.getAuthTag(), data]).toString("base64url");
}

function decrypt(value: string): string {
  const bytes = Buffer.from(value, "base64url");
  const decipher = createDecipheriv("aes-256-gcm", encryptionKey(), bytes.subarray(0, 12));
  decipher.setAuthTag(bytes.subarray(12, 28));
  return Buffer.concat([decipher.update(bytes.subarray(28)), decipher.final()]).toString("utf8");
}

export async function createBrowserSession(backendUserId: string, token: string, expiresAt: string) {
  await clearBrowserSession();
  const cookieValue = randomBytes(32).toString("base64url");
  const expiry = new Date(expiresAt);
  await prisma.browserSession.create({ data: {
    id: hash(cookieValue), backendUserId, encryptedToken: encrypt(token), expiresAt: expiry,
  } });
  (await cookies()).set(COOKIE_NAME, cookieValue, {
    httpOnly: true, sameSite: "lax", secure: process.env.NODE_ENV === "production",
    path: "/", expires: expiry,
  });
}

export async function getBrowserSession() {
  const value = (await cookies()).get(COOKIE_NAME)?.value;
  if (!value) return null;
  const row = await prisma.browserSession.findUnique({ where: { id: hash(value) } });
  if (!row || row.expiresAt <= new Date()) return null;
  try {
    return { backendUserId: row.backendUserId, token: decrypt(row.encryptedToken) };
  } catch {
    return null;
  }
}

export async function clearBrowserSession() {
  const value = (await cookies()).get(COOKIE_NAME)?.value;
  if (value) await prisma.browserSession.deleteMany({ where: { id: hash(value) } });
  (await cookies()).delete(COOKIE_NAME);
}
