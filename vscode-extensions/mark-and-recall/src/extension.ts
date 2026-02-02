import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';

interface Mark {
    name: string | undefined; // undefined for anonymous marks
    filePath: string;
    line: number;
}

interface MarkWithIndex extends Mark {
    index: number;
}

// Decoration types for marks 1-9
const markDecorationTypes: vscode.TextEditorDecorationType[] = [];

// Decoration type for marks 10+ (star)
let starDecorationType: vscode.TextEditorDecorationType;

// Line highlight decoration
let lineHighlightDecoration: vscode.TextEditorDecorationType;

// File watcher for marks.md
let marksFileWatcher: vscode.FileSystemWatcher | undefined;

// Debounce timer for updating marks.md
let updateMarksDebounceTimer: NodeJS.Timeout | undefined;

// Flag to prevent recursive updates when we write to marks.md
let isUpdatingMarksFile = false;

// Pending mark updates - tracks modified line numbers before writing to file
// Map from mark index to new line number
let pendingMarkUpdates: Map<number, number> | undefined;

function createNumberSvg(num: number): string {
    // Create a smaller SVG with a blue circle and white number
    const svg = `<svg width="12" height="12" xmlns="http://www.w3.org/2000/svg">
        <circle cx="6" cy="6" r="5.5" fill="#2196F3"/>
        <text x="6" y="9" font-size="8" font-family="Arial, sans-serif" font-weight="bold" fill="white" text-anchor="middle">${num}</text>
    </svg>`;
    return 'data:image/svg+xml;base64,' + Buffer.from(svg).toString('base64');
}

function createStarSvg(): string {
    // Create a smaller SVG with a blue circle and white asterisk
    const svg = `<svg width="12" height="12" xmlns="http://www.w3.org/2000/svg">
        <circle cx="6" cy="6" r="5.5" fill="#2196F3"/>
        <text x="6" y="9.5" font-size="10" font-family="Arial, sans-serif" font-weight="bold" fill="white" text-anchor="middle">*</text>
    </svg>`;
    return 'data:image/svg+xml;base64,' + Buffer.from(svg).toString('base64');
}

function initializeDecorations(): void {
    // Create decoration types for marks 1-9
    for (let i = 1; i <= 9; i++) {
        const decorationType = vscode.window.createTextEditorDecorationType({
            gutterIconPath: vscode.Uri.parse(createNumberSvg(i)),
            gutterIconSize: 'contain',
        });
        markDecorationTypes.push(decorationType);
    }

    // Create decoration type for marks 10+
    starDecorationType = vscode.window.createTextEditorDecorationType({
        gutterIconPath: vscode.Uri.parse(createStarSvg()),
        gutterIconSize: 'contain',
    });

    // Create line highlight decoration
    lineHighlightDecoration = vscode.window.createTextEditorDecorationType({
        backgroundColor: 'rgba(33, 150, 243, 0.15)', // Light blue background
        isWholeLine: true,
    });
}

function disposeDecorations(): void {
    for (const decorationType of markDecorationTypes) {
        decorationType.dispose();
    }
    markDecorationTypes.length = 0;

    if (starDecorationType) {
        starDecorationType.dispose();
    }

    if (lineHighlightDecoration) {
        lineHighlightDecoration.dispose();
    }
}

function getMarksQuiet(): MarkWithIndex[] {
    const marksFilePath = getMarksFilePathQuiet();
    if (!marksFilePath) {
        return [];
    }

    const workspaceRoot = path.dirname(marksFilePath);

    if (!fs.existsSync(marksFilePath)) {
        return [];
    }

    let content: string;
    try {
        content = fs.readFileSync(marksFilePath, 'utf-8');
    } catch {
        return [];
    }

    const marks = parseMarksFile(content, workspaceRoot);
    return marks.map((mark, index) => ({ ...mark, index }));
}

function getConfiguredMarksFileName(): string {
    const config = vscode.workspace.getConfiguration('markAndRecall');
    return config.get<string>('marksFilePath', 'marks.md');
}

function getMarksFilePathQuiet(): string | undefined {
    const workspaceFolders = vscode.workspace.workspaceFolders;
    if (!workspaceFolders || workspaceFolders.length === 0) {
        return undefined;
    }

    const workspaceRoot = workspaceFolders[0].uri.fsPath;
    const configuredPath = getConfiguredMarksFileName();

    if (path.isAbsolute(configuredPath)) {
        return configuredPath;
    }
    return path.join(workspaceRoot, configuredPath);
}

function updateDecorationsForEditor(editor: vscode.TextEditor): void {
    if (!editor) {
        return;
    }

    const marks = getMarksQuiet();
    const filePath = editor.document.uri.fsPath;

    // Find all marks for this file
    const fileMarks = marks.filter((mark) => mark.filePath === filePath);

    // Clear all decorations first
    for (const decorationType of markDecorationTypes) {
        editor.setDecorations(decorationType, []);
    }
    editor.setDecorations(starDecorationType, []);
    editor.setDecorations(lineHighlightDecoration, []);

    // Collect decorations
    const lineHighlights: vscode.DecorationOptions[] = [];
    const starDecorations: vscode.DecorationOptions[] = [];

    for (const mark of fileMarks) {
        const line = mark.line - 1; // Convert to 0-based
        if (line < 0 || line >= editor.document.lineCount) {
            continue;
        }

        const range = new vscode.Range(line, 0, line, 0);
        const hoverMessage = mark.name
            ? `Mark ${mark.index + 1}: ${mark.name}`
            : `Mark ${mark.index + 1}`;

        if (mark.index < 9) {
            // Marks 1-9 get numbered icons
            const decorationType = markDecorationTypes[mark.index];
            if (decorationType) {
                editor.setDecorations(decorationType, [{ range, hoverMessage }]);
            }
        } else {
            // Marks 10+ get star icons
            starDecorations.push({ range, hoverMessage });
        }

        lineHighlights.push({ range });
    }

    editor.setDecorations(starDecorationType, starDecorations);
    editor.setDecorations(lineHighlightDecoration, lineHighlights);
}

function updateAllDecorations(): void {
    for (const editor of vscode.window.visibleTextEditors) {
        updateDecorationsForEditor(editor);
    }
}

function setupFileWatcher(context: vscode.ExtensionContext): void {
    // Dispose existing watcher if any
    if (marksFileWatcher) {
        marksFileWatcher.dispose();
        marksFileWatcher = undefined;
    }

    const marksFilePath = getMarksFilePathQuiet();
    if (!marksFilePath) {
        return;
    }

    const configuredPath = getConfiguredMarksFileName();

    // Watch for changes to the configured marks file
    if (path.isAbsolute(configuredPath)) {
        // For absolute paths, watch the specific file
        marksFileWatcher = vscode.workspace.createFileSystemWatcher(
            new vscode.RelativePattern(
                vscode.Uri.file(path.dirname(configuredPath)),
                path.basename(configuredPath)
            )
        );
    } else {
        // For relative paths, watch relative to workspace
        marksFileWatcher = vscode.workspace.createFileSystemWatcher(
            new vscode.RelativePattern(
                vscode.workspace.workspaceFolders![0],
                configuredPath
            )
        );
    }

    marksFileWatcher.onDidChange(() => updateAllDecorations());
    marksFileWatcher.onDidCreate(() => updateAllDecorations());
    marksFileWatcher.onDidDelete(() => updateAllDecorations());

    context.subscriptions.push(marksFileWatcher);
}

function parseMarksFile(content: string, workspaceRoot: string): Mark[] {
    const marks: Mark[] = [];
    const lines = content.split('\n');

    for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed || trimmed.startsWith('#')) {
            // Skip empty lines and comments
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
        // We need to find the first colon that separates name from path
        const firstColonIndex = beforeLineNum.indexOf(':');

        let name: string | undefined;
        let filePath: string;

        if (firstColonIndex !== -1) {
            // Could be named or could just be an absolute path like /home/user/file
            // Check if what's before the first colon looks like a path component
            const potentialName = beforeLineNum.substring(0, firstColonIndex).trim();
            const potentialPath = beforeLineNum.substring(firstColonIndex + 1).trim();

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
            // No colon in the path part - anonymous relative path
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

function getMarksFilePath(): string | undefined {
    const workspaceFolders = vscode.workspace.workspaceFolders;
    if (!workspaceFolders || workspaceFolders.length === 0) {
        vscode.window.showErrorMessage('No workspace folder open');
        return undefined;
    }

    const workspaceRoot = workspaceFolders[0].uri.fsPath;
    const configuredPath = getConfiguredMarksFileName();

    if (path.isAbsolute(configuredPath)) {
        return configuredPath;
    }
    return path.join(workspaceRoot, configuredPath);
}

async function openMarks(): Promise<void> {
    const marksFilePath = getMarksFilePath();
    if (!marksFilePath) {
        return;
    }

    const uri = vscode.Uri.file(marksFilePath);

    // Create the file if it doesn't exist
    if (!fs.existsSync(marksFilePath)) {
        const template = `# Mark and Recall File
#
# Named marks (name: path:line) - user-specified
# mymark: src/utils.ts:10
#
# Symbol marks (@symbol: path:line) - auto-detected from code
# @parseConfig: src/utils.ts:42
#
# Anonymous marks (path:line)
# src/helpers.ts:18

`;
        fs.writeFileSync(marksFilePath, template, 'utf-8');
    }

    const document = await vscode.workspace.openTextDocument(uri);
    await vscode.window.showTextDocument(document);
}

async function recall(): Promise<void> {
    const marksFilePath = getMarksFilePath();
    if (!marksFilePath) {
        return;
    }

    const workspaceRoot = path.dirname(marksFilePath);

    if (!fs.existsSync(marksFilePath)) {
        vscode.window.showWarningMessage(
            `No marks.md file found in workspace root (${marksFilePath})`
        );
        return;
    }

    let content: string;
    try {
        content = fs.readFileSync(marksFilePath, 'utf-8');
    } catch (err) {
        vscode.window.showErrorMessage(`Failed to read marks.md: ${err}`);
        return;
    }

    const marks = parseMarksFile(content, workspaceRoot);

    if (marks.length === 0) {
        vscode.window.showInformationMessage('No marks found in marks.md');
        return;
    }

    // Create items with index for identification
    const items: (vscode.QuickPickItem & { markIndex: number })[] = marks.map((mark, index) => {
        const displayName = mark.name || path.basename(mark.filePath);
        const indexLabel = index < 9 ? `[${index + 1}] ` : '[*] ';
        return {
            label: indexLabel + displayName,
            description: `${mark.filePath}:${mark.line}`,
            markIndex: index,
        };
    });

    const selected = await vscode.window.showQuickPick(items, {
        placeHolder: 'Select a mark to navigate to',
        matchOnDescription: true,
    });

    if (!selected) {
        return;
    }

    await navigateToMark(marks[selected.markIndex]);
}

async function navigateToMark(mark: Mark): Promise<void> {
    try {
        const uri = vscode.Uri.file(mark.filePath);
        const document = await vscode.workspace.openTextDocument(uri);
        const editor = await vscode.window.showTextDocument(document);

        // Line numbers in marks.md are 1-based, VS Code positions are 0-based
        const position = new vscode.Position(mark.line - 1, 0);
        editor.selection = new vscode.Selection(position, position);
        editor.revealRange(
            new vscode.Range(position, position),
            vscode.TextEditorRevealType.InCenter
        );
    } catch (err) {
        vscode.window.showErrorMessage(
            `Failed to open file: ${mark.filePath}\n${err}`
        );
    }
}

function getMarks(): Mark[] | undefined {
    const marksFilePath = getMarksFilePath();
    if (!marksFilePath) {
        return undefined;
    }

    const workspaceRoot = path.dirname(marksFilePath);

    if (!fs.existsSync(marksFilePath)) {
        vscode.window.showWarningMessage(
            `No marks.md file found in workspace root (${marksFilePath})`
        );
        return undefined;
    }

    let content: string;
    try {
        content = fs.readFileSync(marksFilePath, 'utf-8');
    } catch (err) {
        vscode.window.showErrorMessage(`Failed to read marks.md: ${err}`);
        return undefined;
    }

    return parseMarksFile(content, workspaceRoot);
}

async function recallByIndex(args: { index: number }): Promise<void> {
    const marks = getMarks();
    if (!marks) {
        return;
    }

    const index = args?.index ?? 0;

    if (index < 0 || index >= marks.length) {
        vscode.window.showWarningMessage(
            `Mark index ${index + 1} out of range (have ${marks.length} marks)`
        );
        return;
    }

    await navigateToMark(marks[index]);
}

async function getSymbolAtPosition(
    document: vscode.TextDocument,
    position: vscode.Position
): Promise<string | undefined> {
    try {
        const symbols = await vscode.commands.executeCommand<vscode.DocumentSymbol[]>(
            'vscode.executeDocumentSymbolProvider',
            document.uri
        );

        if (!symbols || symbols.length === 0) {
            return undefined;
        }

        // Find symbol whose definition line matches the cursor line
        function findSymbolOnDefinitionLine(
            syms: vscode.DocumentSymbol[],
            line: number
        ): vscode.DocumentSymbol | undefined {
            for (const sym of syms) {
                // Check if cursor is on the symbol's definition line
                if (sym.selectionRange.start.line === line) {
                    return sym;
                }
                // Check children recursively
                const childMatch = findSymbolOnDefinitionLine(sym.children, line);
                if (childMatch) {
                    return childMatch;
                }
            }
            return undefined;
        }

        const symbol = findSymbolOnDefinitionLine(symbols, position.line);
        return symbol?.name;
    } catch {
        return undefined;
    }
}

async function addMark(prepend: boolean, named: boolean): Promise<void> {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
        vscode.window.showErrorMessage('No active editor');
        return;
    }

    const marksFilePath = getMarksFilePath();
    if (!marksFilePath) {
        return;
    }

    const workspaceRoot = path.dirname(marksFilePath);
    const filePath = editor.document.uri.fsPath;
    const line = editor.selection.active.line + 1; // Convert to 1-based

    // Check for duplicate mark at same location
    const existingMarks = getMarksQuiet();
    const duplicate = existingMarks.find(
        (m) => m.filePath === filePath && m.line === line
    );
    if (duplicate) {
        const markName = duplicate.name || `${duplicate.filePath}:${duplicate.line}`;
        vscode.window.showInformationMessage(
            `Mark already exists at this location: ${markName}`
        );
        return;
    }

    // Use relative path if within workspace, otherwise absolute
    const relativePath = path.relative(workspaceRoot, filePath);
    const isWithinWorkspace = !relativePath.startsWith('..');
    const displayPath = isWithinWorkspace ? relativePath : filePath;

    let markEntry: string;

    if (named) {
        // Try to get symbol at cursor, fall back to filename
        const symbolName = await getSymbolAtPosition(
            editor.document,
            editor.selection.active
        );
        // Prefix symbol names with @ to distinguish from user-specified names
        const suggestedName = symbolName
            ? `@${symbolName}`
            : path.basename(filePath, path.extname(filePath));

        const name = await vscode.window.showInputBox({
            prompt: 'Enter a name for this mark',
            value: suggestedName,
            validateInput: (value) => {
                if (!value.trim()) {
                    return 'Name cannot be empty';
                }
                if (value.includes(':')) {
                    return 'Name cannot contain colons';
                }
                return null;
            },
        });

        if (!name) {
            return;
        }

        markEntry = `${name}: ${displayPath}:${line}\n`;
    } else {
        // Auto-named if symbol available, otherwise anonymous
        const symbolName = await getSymbolAtPosition(
            editor.document,
            editor.selection.active
        );
        if (symbolName) {
            // Prefix with @ to indicate it's a symbol name
            markEntry = `@${symbolName}: ${displayPath}:${line}\n`;
        } else {
            markEntry = `${displayPath}:${line}\n`;
        }
    }

    // Read existing content or create new file
    let content = '';
    if (fs.existsSync(marksFilePath)) {
        content = fs.readFileSync(marksFilePath, 'utf-8');
    }

    let newContent: string;
    if (prepend) {
        // Find the end of the header (lines starting with #) and prepend after
        const lines = content.split('\n');
        let insertIndex = 0;
        for (let i = 0; i < lines.length; i++) {
            if (lines[i].trim().startsWith('#') || lines[i].trim() === '') {
                insertIndex = i + 1;
            } else {
                break;
            }
        }
        lines.splice(insertIndex, 0, markEntry.trimEnd());
        newContent = lines.join('\n');
    } else {
        // Append to end
        if (content && !content.endsWith('\n')) {
            content += '\n';
        }
        newContent = content + markEntry;
    }

    try {
        fs.writeFileSync(marksFilePath, newContent, 'utf-8');
        vscode.window.showInformationMessage('Mark added');
        // Update decorations immediately
        updateAllDecorations();
    } catch (err) {
        vscode.window.showErrorMessage(`Failed to write marks.md: ${err}`);
    }
}

async function prependMark(): Promise<void> {
    await addMark(true, false);
}

async function prependNamedMark(): Promise<void> {
    await addMark(true, true);
}

async function appendMark(): Promise<void> {
    await addMark(false, false);
}

async function appendNamedMark(): Promise<void> {
    await addMark(false, true);
}

async function deleteMarkAtCursor(): Promise<void> {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
        vscode.window.showErrorMessage('No active editor');
        return;
    }

    const marksFilePath = getMarksFilePathQuiet();
    if (!marksFilePath || !fs.existsSync(marksFilePath)) {
        vscode.window.showWarningMessage('No marks.md file found');
        return;
    }

    const currentFilePath = editor.document.uri.fsPath;
    const currentLine = editor.selection.active.line + 1; // Convert to 1-based

    const marks = getMarksQuiet();

    // Find mark at current position
    const markToDelete = marks.find(
        (m) => m.filePath === currentFilePath && m.line === currentLine
    );

    if (!markToDelete) {
        vscode.window.showInformationMessage('No mark at current line');
        return;
    }

    // Read and modify marks.md
    let content: string;
    try {
        content = fs.readFileSync(marksFilePath, 'utf-8');
    } catch (err) {
        vscode.window.showErrorMessage(`Failed to read marks.md: ${err}`);
        return;
    }

    const lines = content.split('\n');
    let markIndex = 0;
    let lineToDelete = -1;

    for (let i = 0; i < lines.length; i++) {
        const trimmed = lines[i].trim();
        if (!trimmed || trimmed.startsWith('#')) {
            continue;
        }

        // Check if this is a valid mark line
        const lastColonIndex = trimmed.lastIndexOf(':');
        if (lastColonIndex === -1) {
            continue;
        }

        const lineStr = trimmed.substring(lastColonIndex + 1).trim();
        const lineNum = parseInt(lineStr, 10);
        if (isNaN(lineNum)) {
            continue;
        }

        if (markIndex === markToDelete.index) {
            lineToDelete = i;
            break;
        }
        markIndex++;
    }

    if (lineToDelete === -1) {
        vscode.window.showErrorMessage('Could not find mark in marks.md');
        return;
    }

    // Remove the line
    lines.splice(lineToDelete, 1);
    const newContent = lines.join('\n');

    try {
        isUpdatingMarksFile = true;
        fs.writeFileSync(marksFilePath, newContent, 'utf-8');
        vscode.window.showInformationMessage('Mark deleted');
        updateAllDecorations();
    } catch (err) {
        vscode.window.showErrorMessage(`Failed to write marks.md: ${err}`);
    } finally {
        isUpdatingMarksFile = false;
    }
}

async function deleteAllMarksInFile(): Promise<void> {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
        vscode.window.showErrorMessage('No active editor');
        return;
    }

    const marksFilePath = getMarksFilePathQuiet();
    if (!marksFilePath || !fs.existsSync(marksFilePath)) {
        vscode.window.showWarningMessage('No marks.md file found');
        return;
    }

    const currentFilePath = editor.document.uri.fsPath;
    const marks = getMarksQuiet();

    // Find all marks in current file
    const marksToDelete = marks.filter((m) => m.filePath === currentFilePath);

    if (marksToDelete.length === 0) {
        vscode.window.showInformationMessage('No marks in current file');
        return;
    }

    // Get the indices to delete (in reverse order to maintain correct indices)
    const indicesToDelete = new Set(marksToDelete.map((m) => m.index));

    // Read and modify marks.md
    let content: string;
    try {
        content = fs.readFileSync(marksFilePath, 'utf-8');
    } catch (err) {
        vscode.window.showErrorMessage(`Failed to read marks.md: ${err}`);
        return;
    }

    const lines = content.split('\n');
    const newLines: string[] = [];
    let markIndex = 0;

    for (let i = 0; i < lines.length; i++) {
        const trimmed = lines[i].trim();
        if (!trimmed || trimmed.startsWith('#')) {
            newLines.push(lines[i]);
            continue;
        }

        // Check if this is a valid mark line
        const lastColonIndex = trimmed.lastIndexOf(':');
        if (lastColonIndex === -1) {
            newLines.push(lines[i]);
            continue;
        }

        const lineStr = trimmed.substring(lastColonIndex + 1).trim();
        const lineNum = parseInt(lineStr, 10);
        if (isNaN(lineNum)) {
            newLines.push(lines[i]);
            continue;
        }

        // This is a valid mark - only keep if not in delete set
        if (!indicesToDelete.has(markIndex)) {
            newLines.push(lines[i]);
        }
        markIndex++;
    }

    const newContent = newLines.join('\n');

    try {
        isUpdatingMarksFile = true;
        fs.writeFileSync(marksFilePath, newContent, 'utf-8');
        vscode.window.showInformationMessage(
            `Deleted ${marksToDelete.length} mark(s) from current file`
        );
        updateAllDecorations();
    } catch (err) {
        vscode.window.showErrorMessage(`Failed to write marks.md: ${err}`);
    } finally {
        isUpdatingMarksFile = false;
    }
}

async function gotoPreviousMark(): Promise<void> {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
        vscode.window.showErrorMessage('No active editor');
        return;
    }

    const currentFilePath = editor.document.uri.fsPath;
    const currentLine = editor.selection.active.line + 1; // Convert to 1-based

    const marks = getMarksQuiet();
    const fileMarks = marks
        .filter((m) => m.filePath === currentFilePath)
        .sort((a, b) => a.line - b.line);

    if (fileMarks.length === 0) {
        vscode.window.showInformationMessage('No marks in current file');
        return;
    }

    // Find nearest mark above current line
    let targetMark: MarkWithIndex | undefined;
    for (let i = fileMarks.length - 1; i >= 0; i--) {
        if (fileMarks[i].line < currentLine) {
            targetMark = fileMarks[i];
            break;
        }
    }

    // Wrap to bottom if none above
    if (!targetMark) {
        targetMark = fileMarks[fileMarks.length - 1];
    }

    await navigateToMark(targetMark);
}

async function gotoNextMark(): Promise<void> {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
        vscode.window.showErrorMessage('No active editor');
        return;
    }

    const currentFilePath = editor.document.uri.fsPath;
    const currentLine = editor.selection.active.line + 1; // Convert to 1-based

    const marks = getMarksQuiet();
    const fileMarks = marks
        .filter((m) => m.filePath === currentFilePath)
        .sort((a, b) => a.line - b.line);

    if (fileMarks.length === 0) {
        vscode.window.showInformationMessage('No marks in current file');
        return;
    }

    // Find nearest mark below current line
    let targetMark: MarkWithIndex | undefined;
    for (const mark of fileMarks) {
        if (mark.line > currentLine) {
            targetMark = mark;
            break;
        }
    }

    // Wrap to top if none below
    if (!targetMark) {
        targetMark = fileMarks[0];
    }

    await navigateToMark(targetMark);
}

async function updateSymbolMarksInFile(): Promise<void> {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
        vscode.window.showErrorMessage('No active editor');
        return;
    }

    const marksFilePath = getMarksFilePathQuiet();
    if (!marksFilePath || !fs.existsSync(marksFilePath)) {
        vscode.window.showWarningMessage('No marks.md file found');
        return;
    }

    const currentFilePath = editor.document.uri.fsPath;
    const marks = getMarksQuiet();

    // Find symbol marks (starting with @) in current file
    const symbolMarks = marks.filter(
        (m) => m.filePath === currentFilePath && m.name && m.name.startsWith('@')
    );

    if (symbolMarks.length === 0) {
        vscode.window.showInformationMessage('No symbol marks in current file');
        return;
    }

    // Get all symbols in the document
    let symbols: vscode.DocumentSymbol[] | undefined;
    try {
        symbols = await vscode.commands.executeCommand<vscode.DocumentSymbol[]>(
            'vscode.executeDocumentSymbolProvider',
            editor.document.uri
        );
    } catch {
        vscode.window.showErrorMessage('Could not get symbols for this file');
        return;
    }

    if (!symbols || symbols.length === 0) {
        vscode.window.showWarningMessage('No symbols found in current file');
        return;
    }

    // Flatten all symbols with their definition lines
    interface FlatSymbol {
        name: string;
        line: number; // 1-based
    }
    const flatSymbols: FlatSymbol[] = [];

    function collectSymbols(syms: vscode.DocumentSymbol[]): void {
        for (const sym of syms) {
            flatSymbols.push({
                name: sym.name,
                line: sym.selectionRange.start.line + 1, // Convert to 1-based
            });
            collectSymbols(sym.children);
        }
    }
    collectSymbols(symbols);

    // Track updates
    const updates: Map<number, number> = new Map(); // markIndex -> newLine

    for (const mark of symbolMarks) {
        const symbolName = mark.name!.substring(1); // Remove @ prefix

        // Find all symbols with matching name
        const matchingSymbols = flatSymbols.filter((s) => s.name === symbolName);

        if (matchingSymbols.length === 0) {
            // Symbol not found - keep old line
            continue;
        }

        // Pick the closest one to the mark's current line
        let closest = matchingSymbols[0];
        let closestDistance = Math.abs(closest.line - mark.line);

        for (const sym of matchingSymbols) {
            const distance = Math.abs(sym.line - mark.line);
            if (distance < closestDistance) {
                closest = sym;
                closestDistance = distance;
            }
        }

        if (closest.line !== mark.line) {
            updates.set(mark.index, closest.line);
        }
    }

    if (updates.size === 0) {
        vscode.window.showInformationMessage('All symbol marks are up to date');
        return;
    }

    // Read and update marks.md
    let content: string;
    try {
        content = fs.readFileSync(marksFilePath, 'utf-8');
    } catch (err) {
        vscode.window.showErrorMessage(`Failed to read marks.md: ${err}`);
        return;
    }

    const lines = content.split('\n');
    let markIndex = 0;

    for (let i = 0; i < lines.length; i++) {
        const trimmed = lines[i].trim();
        if (!trimmed || trimmed.startsWith('#')) {
            continue;
        }

        // Check if this is a valid mark line
        const lastColonIndex = trimmed.lastIndexOf(':');
        if (lastColonIndex === -1) {
            continue;
        }

        const lineStr = trimmed.substring(lastColonIndex + 1).trim();
        const lineNum = parseInt(lineStr, 10);
        if (isNaN(lineNum)) {
            continue;
        }

        // Check if we need to update this mark
        const newLine = updates.get(markIndex);
        if (newLine !== undefined) {
            const beforeLine = trimmed.substring(0, lastColonIndex);
            lines[i] = `${beforeLine}:${newLine}`;
        }
        markIndex++;
    }

    const newContent = lines.join('\n');

    try {
        isUpdatingMarksFile = true;
        fs.writeFileSync(marksFilePath, newContent, 'utf-8');
        vscode.window.showInformationMessage(
            `Updated ${updates.size} symbol mark(s)`
        );
        updateAllDecorations();
    } catch (err) {
        vscode.window.showErrorMessage(`Failed to write marks.md: ${err}`);
    } finally {
        isUpdatingMarksFile = false;
    }
}

async function selectMarksFile(): Promise<void> {
    const workspaceFolders = vscode.workspace.workspaceFolders;
    if (!workspaceFolders || workspaceFolders.length === 0) {
        vscode.window.showErrorMessage('No workspace folder open');
        return;
    }

    const workspaceRoot = workspaceFolders[0].uri.fsPath;
    const currentPath = getConfiguredMarksFileName();

    interface MarksFileOption extends vscode.QuickPickItem {
        action: 'browse' | 'enter' | 'reset' | 'select';
        filePath?: string;
    }

    const options: MarksFileOption[] = [
        {
            label: '$(folder-opened) Browse for file...',
            description: 'Select an existing file or create a new one',
            action: 'browse',
        },
        {
            label: '$(edit) Enter path manually...',
            description: 'Type a relative or absolute path',
            action: 'enter',
        },
        {
            label: '$(discard) Reset to default',
            description: 'Use marks.md in workspace root',
            action: 'reset',
        },
    ];

    // Add separator and current file info
    options.push({
        label: '',
        kind: vscode.QuickPickItemKind.Separator,
        action: 'select',
    });

    options.push({
        label: `$(file) Current: ${currentPath}`,
        description: path.isAbsolute(currentPath)
            ? currentPath
            : path.join(workspaceRoot, currentPath),
        action: 'select',
        filePath: currentPath,
    });

    // Find existing .md files that could be marks files
    try {
        const mdFiles = await vscode.workspace.findFiles('**/*.md', '**/node_modules/**', 20);
        const existingFiles = mdFiles
            .map((uri) => {
                const relativePath = path.relative(workspaceRoot, uri.fsPath);
                return {
                    label: `$(file) ${relativePath}`,
                    description: uri.fsPath,
                    action: 'select' as const,
                    filePath: relativePath,
                };
            })
            .filter((item) => item.filePath !== currentPath)
            .sort((a, b) => a.filePath!.localeCompare(b.filePath!));

        if (existingFiles.length > 0) {
            options.push({
                label: 'Existing markdown files',
                kind: vscode.QuickPickItemKind.Separator,
                action: 'select',
            });
            options.push(...existingFiles);
        }
    } catch {
        // Ignore errors finding files
    }

    const selected = await vscode.window.showQuickPick(options, {
        placeHolder: 'Select marks file location',
        matchOnDescription: true,
    });

    if (!selected) {
        return;
    }

    let newPath: string | undefined;

    switch (selected.action) {
        case 'browse': {
            const result = await vscode.window.showSaveDialog({
                defaultUri: vscode.Uri.file(path.join(workspaceRoot, 'marks.md')),
                filters: {
                    'Markdown files': ['md'],
                    'All files': ['*'],
                },
                title: 'Select Marks File',
            });
            if (result) {
                // Use relative path if within workspace
                const relativePath = path.relative(workspaceRoot, result.fsPath);
                newPath = relativePath.startsWith('..')
                    ? result.fsPath
                    : relativePath;
            }
            break;
        }
        case 'enter': {
            const input = await vscode.window.showInputBox({
                prompt: 'Enter path to marks file (relative to workspace or absolute)',
                value: currentPath,
                validateInput: (value) => {
                    if (!value.trim()) {
                        return 'Path cannot be empty';
                    }
                    return null;
                },
            });
            if (input) {
                newPath = input.trim();
            }
            break;
        }
        case 'reset':
            newPath = 'marks.md';
            break;
        case 'select':
            if (selected.filePath && selected.filePath !== currentPath) {
                newPath = selected.filePath;
            }
            break;
    }

    if (newPath !== undefined) {
        const config = vscode.workspace.getConfiguration('markAndRecall');
        await config.update('marksFilePath', newPath, vscode.ConfigurationTarget.Workspace);
        vscode.window.showInformationMessage(`Marks file set to: ${newPath}`);
    }
}

function handleDocumentChange(event: vscode.TextDocumentChangeEvent): void {
    // Skip if we're currently updating marks.md ourselves
    if (isUpdatingMarksFile) {
        return;
    }

    const marksFilePath = getMarksFilePathQuiet();
    if (!marksFilePath) {
        return;
    }

    // Don't track changes to marks.md itself
    if (event.document.uri.fsPath === marksFilePath) {
        return;
    }

    const changedFilePath = event.document.uri.fsPath;

    // Initialize pending updates from file if not already tracking
    if (!pendingMarkUpdates) {
        const marks = getMarksQuiet();
        pendingMarkUpdates = new Map();
        for (const mark of marks) {
            pendingMarkUpdates.set(mark.index, mark.line);
        }
    }

    // Get original marks to know which ones point to this file
    const marks = getMarksQuiet();
    const affectedMarkIndices = marks
        .filter((m) => m.filePath === changedFilePath)
        .map((m) => m.index);

    if (affectedMarkIndices.length === 0) {
        return;
    }

    // Calculate line adjustments for each change
    let needsUpdate = false;
    for (const change of event.contentChanges) {
        const startLine = change.range.start.line;
        const endLine = change.range.end.line;
        const newLineCount = (change.text.match(/\n/g) || []).length;
        const oldLineCount = endLine - startLine;
        const lineDelta = newLineCount - oldLineCount;

        if (lineDelta === 0) {
            continue;
        }

        // Adjust mark line numbers in pending updates
        for (const markIndex of affectedMarkIndices) {
            const currentLine = pendingMarkUpdates.get(markIndex);
            if (currentLine === undefined) {
                continue;
            }

            const markLine = currentLine - 1; // Convert to 0-based

            if (markLine > endLine) {
                // Mark is after the change - shift it
                pendingMarkUpdates.set(markIndex, currentLine + lineDelta);
                needsUpdate = true;
            } else if (markLine >= startLine && markLine <= endLine && lineDelta < 0) {
                // Mark is within a deleted range - move to start of deletion
                pendingMarkUpdates.set(markIndex, startLine + 1); // Convert back to 1-based
                needsUpdate = true;
            }
        }
    }

    if (needsUpdate) {
        // Debounce the update to avoid too many writes
        if (updateMarksDebounceTimer) {
            clearTimeout(updateMarksDebounceTimer);
        }
        updateMarksDebounceTimer = setTimeout(() => {
            updateMarksFileWithNewLines(marksFilePath);
            pendingMarkUpdates = undefined; // Reset after writing
        }, 500);
    }
}

function updateMarksFileWithNewLines(marksFilePath: string): void {
    if (!pendingMarkUpdates || !fs.existsSync(marksFilePath)) {
        return;
    }

    let content: string;
    try {
        content = fs.readFileSync(marksFilePath, 'utf-8');
    } catch {
        return;
    }

    const lines = content.split('\n');
    let markIndex = 0;

    for (let i = 0; i < lines.length; i++) {
        const trimmed = lines[i].trim();
        if (!trimmed || trimmed.startsWith('#')) {
            continue;
        }

        // Parse this line to see if it's a valid mark
        const colonIndex = trimmed.indexOf(':');
        if (colonIndex === -1) {
            continue;
        }

        const rest = trimmed.substring(colonIndex + 1).trim();
        const lastColonIndex = rest.lastIndexOf(':');
        if (lastColonIndex === -1) {
            continue;
        }

        const lineStr = rest.substring(lastColonIndex + 1).trim();
        const lineNum = parseInt(lineStr, 10);
        if (isNaN(lineNum)) {
            continue;
        }

        // This is a valid mark - check if we need to update it
        const newLine = pendingMarkUpdates.get(markIndex);
        if (newLine !== undefined && newLine !== lineNum) {
            const name = trimmed.substring(0, colonIndex).trim();
            const filePath = rest.substring(0, lastColonIndex).trim();

            // Reconstruct the line with the new line number
            lines[i] = `${name}: ${filePath}:${newLine}`;
        }
        markIndex++;
    }

    const newContent = lines.join('\n');
    if (newContent !== content) {
        try {
            isUpdatingMarksFile = true;
            fs.writeFileSync(marksFilePath, newContent, 'utf-8');
        } catch {
            // Silently fail - don't interrupt user's work
        } finally {
            isUpdatingMarksFile = false;
        }
    }
}

export function activate(context: vscode.ExtensionContext): void {
    // Initialize decorations
    initializeDecorations();

    // Register commands
    context.subscriptions.push(
        vscode.commands.registerCommand('mark-and-recall.recall', recall),
        vscode.commands.registerCommand('mark-and-recall.openMarks', openMarks),
        vscode.commands.registerCommand('mark-and-recall.prependMark', prependMark),
        vscode.commands.registerCommand('mark-and-recall.prependNamedMark', prependNamedMark),
        vscode.commands.registerCommand('mark-and-recall.appendMark', appendMark),
        vscode.commands.registerCommand('mark-and-recall.appendNamedMark', appendNamedMark),
        vscode.commands.registerCommand('mark-and-recall.deleteMarkAtCursor', deleteMarkAtCursor),
        vscode.commands.registerCommand('mark-and-recall.deleteAllMarksInFile', deleteAllMarksInFile),
        vscode.commands.registerCommand('mark-and-recall.gotoPreviousMark', gotoPreviousMark),
        vscode.commands.registerCommand('mark-and-recall.gotoNextMark', gotoNextMark),
        vscode.commands.registerCommand('mark-and-recall.updateSymbolMarks', updateSymbolMarksInFile),
        vscode.commands.registerCommand('mark-and-recall.recallByIndex', recallByIndex),
        vscode.commands.registerCommand('mark-and-recall.selectMarksFile', selectMarksFile)
    );

    // Set up file watcher for marks file
    setupFileWatcher(context);

    // Handle configuration changes
    context.subscriptions.push(
        vscode.workspace.onDidChangeConfiguration((event) => {
            if (event.affectsConfiguration('markAndRecall.marksFilePath')) {
                // Recreate file watcher for new path
                setupFileWatcher(context);
                // Update decorations with new marks file
                updateAllDecorations();
            }
        })
    );

    // Update decorations when active editor changes
    context.subscriptions.push(
        vscode.window.onDidChangeActiveTextEditor((editor) => {
            if (editor) {
                updateDecorationsForEditor(editor);
            }
        })
    );

    // Update decorations when visible editors change
    context.subscriptions.push(
        vscode.window.onDidChangeVisibleTextEditors(() => {
            updateAllDecorations();
        })
    );

    // Update decorations when document is saved (in case marks.md is edited)
    context.subscriptions.push(
        vscode.workspace.onDidSaveTextDocument((document) => {
            const marksFilePath = getMarksFilePathQuiet();
            if (marksFilePath && document.uri.fsPath === marksFilePath) {
                updateAllDecorations();
            }
        })
    );

    // Track line changes to update marks.md
    context.subscriptions.push(
        vscode.workspace.onDidChangeTextDocument((event) => {
            handleDocumentChange(event);
        })
    );

    // Initial decoration update
    updateAllDecorations();
}

export function deactivate(): void {
    disposeDecorations();
    if (marksFileWatcher) {
        marksFileWatcher.dispose();
    }
    if (updateMarksDebounceTimer) {
        clearTimeout(updateMarksDebounceTimer);
    }
}
