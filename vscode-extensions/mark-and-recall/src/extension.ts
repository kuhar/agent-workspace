import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';

interface Mark {
    name: string;
    filePath: string;
    line: number;
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

        // Format: name: <path>:<line>
        const colonIndex = trimmed.indexOf(':');
        if (colonIndex === -1) {
            continue;
        }

        const name = trimmed.substring(0, colonIndex).trim();
        const rest = trimmed.substring(colonIndex + 1).trim();

        // Find the last colon to separate path from line number
        const lastColonIndex = rest.lastIndexOf(':');
        if (lastColonIndex === -1) {
            continue;
        }

        const filePath = rest.substring(0, lastColonIndex).trim();
        const lineStr = rest.substring(lastColonIndex + 1).trim();
        const lineNum = parseInt(lineStr, 10);

        if (isNaN(lineNum)) {
            continue;
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
    return path.join(workspaceRoot, 'marks.md');
}

async function openMarks(): Promise<void> {
    const marksFilePath = getMarksFilePath();
    if (!marksFilePath) {
        return;
    }

    const uri = vscode.Uri.file(marksFilePath);

    // Create the file if it doesn't exist
    if (!fs.existsSync(marksFilePath)) {
        fs.writeFileSync(marksFilePath, '# Marks\n\n', 'utf-8');
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

    const items: vscode.QuickPickItem[] = marks.map((mark) => ({
        label: mark.name,
        description: `${mark.filePath}:${mark.line}`,
    }));

    const selected = await vscode.window.showQuickPick(items, {
        placeHolder: 'Select a mark to navigate to',
        matchOnDescription: true,
    });

    if (!selected) {
        return;
    }

    const mark = marks.find((m) => m.name === selected.label);
    if (!mark) {
        return;
    }

    await navigateToMark(mark);
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

async function addMark(prepend: boolean): Promise<void> {
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

    // Use relative path if within workspace, otherwise absolute
    const relativePath = path.relative(workspaceRoot, filePath);
    const isWithinWorkspace = !relativePath.startsWith('..');
    const displayPath = isWithinWorkspace ? relativePath : filePath;

    // Suggest a name based on the filename
    const suggestedName = path.basename(filePath, path.extname(filePath));

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

    const markEntry = `${name}: ${displayPath}:${line}\n`;

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
        vscode.window.showInformationMessage(`Mark "${name}" added`);
    } catch (err) {
        vscode.window.showErrorMessage(`Failed to write marks.md: ${err}`);
    }
}

async function prependMark(): Promise<void> {
    await addMark(true);
}

async function appendMark(): Promise<void> {
    await addMark(false);
}

export function activate(context: vscode.ExtensionContext): void {
    context.subscriptions.push(
        vscode.commands.registerCommand('mark-and-recall.recall', recall),
        vscode.commands.registerCommand('mark-and-recall.openMarks', openMarks),
        vscode.commands.registerCommand('mark-and-recall.prependMark', prependMark),
        vscode.commands.registerCommand('mark-and-recall.appendMark', appendMark),
        vscode.commands.registerCommand('mark-and-recall.recallByIndex', recallByIndex)
    );
}

export function deactivate(): void {
    // Nothing to clean up
}
