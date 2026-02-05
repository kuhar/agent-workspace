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
exports.AI_TOOLS = exports.INSTALLABLES = void 0;
exports.detectTools = detectTools;
exports.getTargetPath = getTargetPath;
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
exports.INSTALLABLES = [
    { name: 'mark-and-recall', kind: 'skill', resourceFile: 'mark-and-recall.md' },
    { name: 'codebase-cartographer', kind: 'agent', resourceFile: 'codebase-cartographer.md' },
];
exports.AI_TOOLS = [
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
function detectTools(home, tools = exports.AI_TOOLS) {
    return tools.filter((tool) => {
        const configDir = path.join(home, tool.detection);
        return fs.existsSync(configDir);
    });
}
function getTargetPath(tool, scope, baseDir, installable) {
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
//# sourceMappingURL=tools.js.map