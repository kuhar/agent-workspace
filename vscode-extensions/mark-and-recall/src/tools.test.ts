import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';
import { detectTools, getTargetPath, AI_TOOLS, INSTALLABLES, Installable } from './tools';

const skillInstallable: Installable = INSTALLABLES.find((i) => i.kind === 'skill')!;
const agentInstallable: Installable = INSTALLABLES.find((i) => i.kind === 'agent')!;

describe('detectTools', () => {
    let tmpDir: string;

    beforeEach(() => {
        tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'tools-test-'));
    });

    afterEach(() => {
        fs.rmSync(tmpDir, { recursive: true });
    });

    it('returns empty array when no tool dirs exist', () => {
        expect(detectTools(tmpDir)).toEqual([]);
    });

    it('detects Claude Code', () => {
        fs.mkdirSync(path.join(tmpDir, '.claude'));
        const detected = detectTools(tmpDir);
        expect(detected).toHaveLength(1);
        expect(detected[0].name).toBe('Claude Code');
    });

    it('detects Cursor', () => {
        fs.mkdirSync(path.join(tmpDir, '.cursor'));
        const detected = detectTools(tmpDir);
        expect(detected).toHaveLength(1);
        expect(detected[0].name).toBe('Cursor');
    });

    it('detects Codex', () => {
        fs.mkdirSync(path.join(tmpDir, '.codex'));
        const detected = detectTools(tmpDir);
        expect(detected).toHaveLength(1);
        expect(detected[0].name).toBe('Codex');
    });

    it('detects multiple tools', () => {
        fs.mkdirSync(path.join(tmpDir, '.claude'));
        fs.mkdirSync(path.join(tmpDir, '.cursor'));
        fs.mkdirSync(path.join(tmpDir, '.codex'));
        const detected = detectTools(tmpDir);
        expect(detected).toHaveLength(3);
        expect(detected.map((t) => t.name)).toEqual(['Claude Code', 'Cursor', 'Codex']);
    });

    it('ignores unrelated directories', () => {
        fs.mkdirSync(path.join(tmpDir, '.vscode'));
        fs.mkdirSync(path.join(tmpDir, '.git'));
        expect(detectTools(tmpDir)).toEqual([]);
    });

    it('accepts custom tools list', () => {
        fs.mkdirSync(path.join(tmpDir, '.custom'));
        const customTools = [{
            name: 'Custom',
            detection: '.custom',
            dirs: {
                skill: { project: '.custom/skills', global: '.custom/skills', layout: 'subdirectory' as const },
                agent: { project: '.custom/agents', global: '.custom/agents', layout: 'flat' as const },
            },
        }];
        const detected = detectTools(tmpDir, customTools);
        expect(detected).toHaveLength(1);
        expect(detected[0].name).toBe('Custom');
    });
});

describe('getTargetPath', () => {
    const claude = AI_TOOLS[0];
    const cursor = AI_TOOLS[1];
    const codex = AI_TOOLS[2];

    describe('Claude Code', () => {
        it('installs skills as subdirectory/SKILL.md', () => {
            expect(getTargetPath(claude, 'project', '/proj', skillInstallable))
                .toBe('/proj/.claude/skills/mark-and-recall/SKILL.md');
        });

        it('installs agents as flat .md files', () => {
            expect(getTargetPath(claude, 'project', '/proj', agentInstallable))
                .toBe('/proj/.claude/agents/codebase-cartographer.md');
        });

        it('uses same structure for global scope', () => {
            expect(getTargetPath(claude, 'global', '/home/user', skillInstallable))
                .toBe('/home/user/.claude/skills/mark-and-recall/SKILL.md');
            expect(getTargetPath(claude, 'global', '/home/user', agentInstallable))
                .toBe('/home/user/.claude/agents/codebase-cartographer.md');
        });
    });

    describe('Cursor', () => {
        it('installs skills as subdirectory/SKILL.md', () => {
            expect(getTargetPath(cursor, 'project', '/proj', skillInstallable))
                .toBe('/proj/.cursor/skills/mark-and-recall/SKILL.md');
        });

        it('installs agents as flat .md files', () => {
            expect(getTargetPath(cursor, 'project', '/proj', agentInstallable))
                .toBe('/proj/.cursor/agents/codebase-cartographer.md');
        });

        it('uses same structure for global scope', () => {
            expect(getTargetPath(cursor, 'global', '/home/user', skillInstallable))
                .toBe('/home/user/.cursor/skills/mark-and-recall/SKILL.md');
            expect(getTargetPath(cursor, 'global', '/home/user', agentInstallable))
                .toBe('/home/user/.cursor/agents/codebase-cartographer.md');
        });
    });

    describe('Codex', () => {
        it('installs skills as subdirectory/SKILL.md under .agents/skills', () => {
            expect(getTargetPath(codex, 'project', '/proj', skillInstallable))
                .toBe('/proj/.agents/skills/mark-and-recall/SKILL.md');
        });

        it('returns undefined for agents (not supported)', () => {
            expect(getTargetPath(codex, 'project', '/proj', agentInstallable))
                .toBeUndefined();
        });

        it('uses same structure for global scope', () => {
            expect(getTargetPath(codex, 'global', '/home/user', skillInstallable))
                .toBe('/home/user/.agents/skills/mark-and-recall/SKILL.md');
            expect(getTargetPath(codex, 'global', '/home/user', agentInstallable))
                .toBeUndefined();
        });
    });
});
