import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const BODY_PATTERN = /<body(?:\s+[^>]*)?>([\s\S]*?)<\/body>/i;
const SCRIPT_PATTERN = /<script\b[^>]*>[\s\S]*?<\/script>/gi;

export function readLegacyBody(filename: string): string {
  const source = readFileSync(
    resolve(process.cwd(), "..", "web", filename),
    "utf8",
  );
  const match = BODY_PATTERN.exec(source);
  if (!match) throw new Error(`Missing body in legacy template: ${filename}`);
  return match[1].replace(SCRIPT_PATTERN, "").trim();
}
