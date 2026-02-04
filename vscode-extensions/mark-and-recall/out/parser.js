"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.parseMarksFile = parseMarksFile;
const path = __importStar(require("path"));
function parseMarksFile(content, workspaceRoot) {
    const marks = [];
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
        let name;
        let filePath;
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
            }
            else {
                // This is an anonymous mark with the full path
                name = undefined;
                filePath = beforeLineNum;
            }
        }
        else {
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
//# sourceMappingURL=parser.js.map