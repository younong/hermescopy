import {
  Archive,
  Braces,
  Code2,
  File,
  FileAudio,
  FileImage,
  FileJson,
  FileSpreadsheet,
  FileText,
  FileType2,
  FileVideo,
  Presentation,
} from "lucide-react";

import { sessionFileType } from "../files";

export function FileTypeIcon({
  className = "h-4 w-4",
  mimeType,
  name,
}: {
  className?: string;
  mimeType?: string;
  name: string;
}) {
  const props = { "aria-hidden": true, className };
  switch (sessionFileType(name, mimeType)) {
    case "html":
      return <Code2 {...props} data-file-type="html" />;
    case "pdf":
      return <FileType2 {...props} data-file-type="pdf" />;
    case "image":
      return <FileImage {...props} data-file-type="image" />;
    case "archive":
      return <Archive {...props} data-file-type="archive" />;
    case "spreadsheet":
      return <FileSpreadsheet {...props} data-file-type="spreadsheet" />;
    case "document":
    case "text":
      return <FileText {...props} data-file-type="document" />;
    case "presentation":
      return <Presentation {...props} data-file-type="presentation" />;
    case "json":
      return <FileJson {...props} data-file-type="json" />;
    case "code":
      return <Braces {...props} data-file-type="code" />;
    case "audio":
      return <FileAudio {...props} data-file-type="audio" />;
    case "video":
      return <FileVideo {...props} data-file-type="video" />;
    default:
      return <File {...props} data-file-type="generic" />;
  }
}
