-- CreateTable
CREATE TABLE "BrowserSession" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "backendUserId" TEXT NOT NULL,
    "encryptedToken" TEXT NOT NULL,
    "expiresAt" DATETIME NOT NULL,
    "createdAt" DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- CreateIndex
CREATE INDEX "BrowserSession_backendUserId_idx" ON "BrowserSession"("backendUserId");

-- CreateIndex
CREATE INDEX "BrowserSession_expiresAt_idx" ON "BrowserSession"("expiresAt");
