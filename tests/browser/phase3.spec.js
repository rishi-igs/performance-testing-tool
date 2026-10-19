const { test, expect } = require("@playwright/test");
const path = require("path");

test("completes the Phase 3 HAR debug workflow", async ({ page }) => {
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));

  await page.goto("/");
  await expect(page).toHaveTitle("Load test console");

  const harPath = path.resolve(__dirname, "../../manual-test-data/phase3-demo.har");
  await page.getByRole("button", { name: /HAR from browser DevTools/ }).click();
  await page.locator('input[type="file"]').setInputFiles(harPath);
  await page.getByRole("button", { name: "Import and review" }).click();

  await expect(page.getByRole("heading", { name: "phase3-demo HAR" })).toBeVisible();
  await page.getByRole("button", { name: "Validate generated plan" }).click();
  await expect(page.getByRole("heading", { name: "Pass · 2/2 requests passed" })).toBeVisible();

  await page.getByRole("button", { name: "Run debug test" }).click();
  await expect(page.getByText(/completed · 2 passed, 0 failed, 0 assertion failures/))
    .toBeVisible({ timeout: 45_000 });
  await expect.poll(async () => page.locator("#debugresults").getByText(/^PASS ·/).count())
    .toBeGreaterThanOrEqual(2);

  expect(pageErrors).toEqual([]);
});
