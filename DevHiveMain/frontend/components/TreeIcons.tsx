export function FolderIcon({ open }: { open: boolean }) {
  return <svg className="tree-icon folder-icon" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
    <path d={open ? "M2 5a2 2 0 0 1 2-2h4l2 2h6a2 2 0 0 1 2 2v1H5a2 2 0 0 0-1.9 1.4L1 16V5Zm3 5h13l-2.2 6.6A2 2 0 0 1 14 18H2l2.1-6.6A2 2 0 0 1 5 10Z" : "M2 5a2 2 0 0 1 2-2h4l2 2h6a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5Z"}/>
  </svg>;
}

export function FileIcon({ name }: { name: string }) {
  const suffix = name.split(".").pop()?.toLowerCase();
  const tone = ["ts", "tsx", "js", "jsx"].includes(suffix || "") ? "script" :
    suffix === "py" ? "python" : ["json", "yaml", "yml", "toml"].includes(suffix || "") ? "config" : "plain";
  return <svg className={`tree-icon file-icon ${tone}`} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
    <path d="M5 2h7l4 4v11a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1Z"/><path d="M12 2v5h4M7 11h6M7 14h5"/>
  </svg>;
}
