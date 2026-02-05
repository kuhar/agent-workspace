import * as fs from 'fs';
import * as path from 'path';

export type InstallableKind = 'skill' | 'agent';

export interface Installable {
    name: string;
    kind: InstallableKind;
    resourceFile: string;
}

export const INSTALLABLES: Installable[] = [
    { name: 'mark-and-recall', kind: 'skill', resourceFile: 'mark-and-recall.md' },
    { name: 'codebase-cartographer', kind: 'agent', resourceFile: 'codebase-cartographer.md' },
];

interface DirEntry { project: string; global: string; layout: 'flat' | 'subdirectory' }

export interface ToolDirConfig {
    skill: DirEntry;
    agent?: DirEntry;
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
            skill: { project: '.claude/skills', global: '.claude/skills', layout: 'subdirectory' },
            agent: { project: '.claude/agents', global: '.claude/agents', layout: 'flat' },
        },
    },
    {
        name: 'Cursor',
        detection: '.cursor',
        dirs: {
            skill: { project: '.cursor/skills', global: '.cursor/skills', layout: 'subdirectory' },
            agent: { project: '.cursor/agents', global: '.cursor/agents', layout: 'flat' },
        },
    },
    {
        name: 'Codex',
        detection: '.codex',
        dirs: {
            skill: { project: '.agents/skills', global: '.agents/skills', layout: 'subdirectory' },
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
): string | undefined {
    const dirConfig = tool.dirs[installable.kind];
    if (!dirConfig) {
        return undefined;
    }
    const dir = scope === 'project' ? dirConfig.project : dirConfig.global;
    const base = path.join(baseDir, dir);
    if (dirConfig.layout === 'subdirectory') {
        return path.join(base, installable.name, 'SKILL.md');
    }
    return path.join(base, `${installable.name}.md`);
}
