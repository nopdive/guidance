import type {GenToken} from "./stitch";

export interface MetricDef {
    name: string,
    units: string,
    description: string,
    isScalar: boolean,
    precision: number,
}

export type MetricVal = string | number | Array<number | string>;

export interface Token {
    text: string,
    prob: number,
    role: string,
    special: boolean,
    extra?: GenToken,
}
export declare type TokenCallback = (token: Token) => number;