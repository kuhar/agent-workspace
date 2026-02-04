import * as path from 'path';

export interface Mark {
    name: string | undefined; // undefined for anonymous marks
    filePath: string;
    line: number;
}

export function parseMarksFile(content: string, workspaceRoot: string): Mark[] {
    const marks: Mark[] = [];
    const lines = content.split('\n');
    let inHtmlComment = false;

    for (const line of lines) {
        const trimmed = line.trim();

        // Handle HTML-style markdown comments (<!-- ... -->)
        if (inHtmlComment) {
            if (trimmed.includes('-->')) {
                inHtmlComment = false;
            }
            continue;
        }

        if (trimmed.startsWith('<!--')) {
            // Check if comment closes on same line
            if (!trimmed.includes('-->')) {
                inHtmlComment = true;
            }
            continue;
        }

        if (!trimmed || trimmed.startsWith('#')) {
            // Skip empty lines and # comments
            continue;
        }

        // Try to parse as named mark first: name: <path>:<line>
        // Then try anonymous mark: <path>:<line>

        // Find the last colon (before the line number)
        const lastColonIndex = trimmed.lastIndexOf(':');
        if (lastColonIndex === -1) {
            continue;
        }

        const lineStr = trimmed.substring(lastColonIndex + 1).trim();
        const lineNum = parseInt(lineStr, 10);
        if (isNaN(lineNum)) {
            continue;
        }

        const beforeLineNum = trimmed.substring(0, lastColonIndex).trim();

        // Now check if there's a name: prefix
        // A named mark has format "name: path" before the line number
        // We need to find the ": " (colon-space) that separates name from path
        // Using ": " instead of just ":" allows C++ namespaces like mlir::foo in names
        const colonSpaceIndex = beforeLineNum.indexOf(': ');

        let name: string | undefined;
        let filePath: string;

        if (colonSpaceIndex !== -1) {
            // Could be named or could just be an absolute path like /home/user/file
            // Check if what's before the colon-space looks like a path component
            const potentialName = beforeLineNum.substring(0, colonSpaceIndex).trim();
            const potentialPath = beforeLineNum.substring(colonSpaceIndex + 2).trim();

            // If potentialPath starts with / or looks like a relative path, it's named
            // If potentialName contains / or \, it's probably part of a path (Windows drive letter case)
            if (potentialName.length > 0 &&
                !potentialName.includes('/') &&
                !potentialName.includes('\\') &&
                potentialPath.length > 0) {
                // This is a named mark
                name = potentialName;
                filePath = potentialPath;
            } else {
                // This is an anonymous mark with the full path
                name = undefined;
                filePath = beforeLineNum;
            }
        } else {
            // No colon-space in the path part - anonymous relative path
            name = undefined;
            filePath = beforeLineNum;
        }

        // Resolve relative paths against workspace root
        const resolvedPath = path.isAbsolute(filePath)
            ? filePath
            : path.join(workspaceRoot, filePath);

        marks.push({
            name,
            filePath: resolvedPath,
            line: lineNum,
        });
    }

    return marks;
}
