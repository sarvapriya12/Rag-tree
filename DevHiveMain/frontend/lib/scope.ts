import type { ScopeFile } from "@/types";

/**
 * Scope selection is a list of paths. A folder path means "everything under it", so selecting a
 * folder never lists its files. These helpers keep that list minimal and correct.
 */
export type ScopeNode = { name: string; path: string; file: boolean; children: ScopeNode[]; files: number; chunks: number };
export type CheckState = "all" | "some" | "none";

export function buildScopeTree(files: ScopeFile[]): ScopeNode {
  const root: ScopeNode = { name: "", path: "", file: false, children: [], files: 0, chunks: 0 };
  const folders = new Map<string, ScopeNode>([["", root]]);
  for (const file of files) {
    const parts = file.path.split("/");
    let parent = root;
    parts.forEach((name, index) => {
      const path = parts.slice(0, index + 1).join("/");
      const isFile = index === parts.length - 1;
      let node = folders.get(path);
      if (!node) {
        node = { name, path, file: isFile, children: [], files: 0, chunks: 0 };
        parent.children.push(node);
        if (!isFile) folders.set(path, node);
      }
      node.files += 1;
      node.chunks += file.chunks;
      parent = node;
    });
    root.files += 1;
    root.chunks += file.chunks;
  }
  const sort = (node: ScopeNode) => {
    node.children.sort((a, b) => Number(a.file) - Number(b.file) || a.name.localeCompare(b.name, undefined, { numeric: true }));
    node.children.forEach(sort);
  };
  sort(root);
  return root;
}

export function covered(path: string, selection: string[]): boolean {
  return selection.some(entry => path === entry || path.startsWith(entry + "/"));
}

export function stateOf(path: string, selection: string[]): CheckState {
  if (covered(path, selection)) return "all";
  return selection.some(entry => entry.startsWith(path + "/")) ? "some" : "none";
}

/** Drop duplicates and entries already covered by a selected parent. */
export function normalize(selection: string[]): string[] {
  const unique = [...new Set(selection)];
  return unique.filter(entry => !unique.some(other => other !== entry && entry.startsWith(other + "/"))).sort();
}

function find(root: ScopeNode, path: string): ScopeNode | null {
  let node: ScopeNode | undefined = root;
  for (const part of path.split("/")) {
    node = node.children.find(child => child.name === part);
    if (!node) return null;
  }
  return node;
}

/** If every child of a folder is selected, select the folder itself instead. */
export function compact(root: ScopeNode, selection: string[]): string[] {
  let next = normalize(selection);
  const visit = (node: ScopeNode): void => {
    node.children.forEach(visit);
    if (node.path && !node.file && node.children.length && !covered(node.path, next)
      && node.children.every(child => covered(child.path, next))) next = normalize([...next, node.path]);
  };
  visit(root);
  return next;
}

/** Click on a row: select it, or deselect it (splitting a selected parent into its other children). */
export function toggle(root: ScopeNode, selection: string[], path: string): string[] {
  if (stateOf(path, selection) !== "all") {
    const rest = selection.filter(entry => !entry.startsWith(path + "/"));
    return compact(root, [...rest, path]);
  }
  const ancestor = selection.filter(entry => path.startsWith(entry + "/"))
    .sort((a, b) => b.length - a.length)[0];
  if (ancestor === undefined) return normalize(selection.filter(entry => entry !== path));
  const next = selection.filter(entry => entry !== ancestor);
  let node = find(root, ancestor);
  for (const part of path.slice(ancestor.length + 1).split("/")) {
    if (!node) break;
    for (const child of node.children) if (child.name !== part) next.push(child.path);
    node = node.children.find(child => child.name === part) ?? null;
  }
  return normalize(next);
}

export function summarize(files: ScopeFile[], selection: string[]): { files: number; chunks: number } {
  const chosen = selection.length ? files.filter(file => covered(file.path, selection)) : files;
  return { files: chosen.length, chunks: chosen.reduce((sum, file) => sum + file.chunks, 0) };
}
