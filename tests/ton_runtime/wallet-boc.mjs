import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { Address, Cell, toNano } from '@ton/core';
import { Blockchain } from '@ton/sandbox';
import { WalletContractV3R2, WalletContractV4 } from '@ton/ton';

const input = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const chain = await Blockchain.create();
const walletType = input.version === 'v4r2' ? WalletContractV4 : WalletContractV3R2;
const wallet = walletType.create({ workchain: 0, publicKey: Buffer.from(input.public, 'hex') });
assert(wallet.address.equals(Address.parse(input.address)), 'StateInit address differs from independent SDK');
const sender = await chain.treasury('funder');
await sender.send({ to: wallet.address, value: toNano('10'), bounce: false });
const before = (await chain.getContract(Address.parse(input.destination))).balance;
const result = await chain.sendMessage(Cell.fromBase64(input.boc));
const own = result.transactions.find(tx => tx.address === BigInt('0x' + wallet.address.hash.toString('hex')));
assert(own && !own.description.aborted, 'Signed wallet message was not accepted');
assert.equal(await chain.openContract(wallet).getSeqno(), 1);
const after = (await chain.getContract(Address.parse(input.destination))).balance;
assert(after > before, 'Recipient did not receive native TON');
let replayRejected = false;
try { await chain.sendMessage(Cell.fromBase64(input.boc)); } catch { replayRejected = true; }
assert(replayRejected, 'Repeated BOC was accepted');
assert.equal(await chain.openContract(wallet).getSeqno(), 1);
assert.equal((await chain.getContract(Address.parse(input.destination))).balance, after);
console.log(JSON.stringify({stateInit: true, signature: true, destination: true, replayRejected: true}));
