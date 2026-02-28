import * as fs from 'fs';
import * as path from 'path';

export interface Installable {
    name: string;
    resourceFile: string;
    extraFiles?: string[];
}

export const INSTALLABLES: Installable[] = [
    { name: 'mark-and-recall', resourceFile: 'mark-and-recall.md', extraFiles: ['validate_marks.py'] },
];

interface DirEntry { project: string; global: string }

export interface ToolDirConfig {
    skill: DirEntry;
}

export interface ToolConfig {
    name: string;
    detection: string;
    dirs: ToolDirConfig;
}

export const AI_TOOLS: ToolConfig[] = [
    {
        name: 'Claude Code',
        detection: '.claude',
        dirs: {
            skill: { project: '.claude/skills', global: '.claude/skills' },
        },
    },
    {
        name: 'Cursor',
        detection: '.cursor',
        dirs: {
            skill: { project: '.cursor/skills', global: '.cursor/skills' },
        },
    },
    {
        name: 'Codex',
        detection: '.codex',
        dirs: {
            skill: { project: '.agents/skills', global: '.agents/skills' },
        },
    },
];

export function detectTools(home: string, tools: ToolConfig[] = AI_TOOLS): ToolConfig[] {
    return tools.filter((tool) => {
        const configDir = path.join(home, tool.detection);
        return fs.existsSync(configDir);
    });
}

export function getTargetPath(
    tool: ToolConfig,
    scope: 'project' | 'global',
    baseDir: string,
    installable: Installable
): string {
    const dirConfig = tool.dirs.skill;
    const dir = scope === 'project' ? dirConfig.project : dirConfig.global;
    return path.join(baseDir, dir, installable.name, 'SKILL.md');
}
