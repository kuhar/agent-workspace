import * as fs from 'fs';
import * as path from 'path';

export interface ToolConfig {
    name: string;
    detection: string;
    projectDir: string;
    globalDir: string;
}

export const AI_TOOLS: ToolConfig[] = [
    {
        name: 'Claude Code',
        detection: '.claude',
        projectDir: '.claude/commands',
        globalDir: '.claude/commands',
    },
    {
        name: 'Cursor',
        detection: '.cursor',
        projectDir: '.cursor/rules',
        globalDir: '.cursor/rules',
    },
    {
        name: 'Codex',
        detection: '.codex',
        projectDir: '.codex',
        globalDir: '.codex',
    },
];

export const SKILL_FILES = ['mark-and-recall.md', 'codebase-cartographer.md'];

export function detectTools(home: string, tools: ToolConfig[] = AI_TOOLS): ToolConfig[] {
    return tools.filter((tool) => {
        const configDir = path.join(home, tool.detection);
        return fs.existsSync(configDir);
    });
}

export function getTargetDir(
    tool: ToolConfig,
    scope: 'project' | 'global',
    baseDir: string
): string {
    const dir = scope === 'project' ? tool.projectDir : tool.globalDir;
    return path.join(baseDir, dir);
}
