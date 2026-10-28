import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { Address, Cell, contractAddress, toNano } from '@ton/core';
import { Blockchain, createShardAccount } from '@ton/sandbox';
import { WalletContractV3R2 } from '@ton/ton';

const input = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const chain = await Blockchain.create();
const owner = WalletContractV3R2.create({ workchain: 0, publicKey: Buffer.from(input.public, 'hex') });
assert(owner.address.equals(Address.parse(input.owner)), 'Owner StateInit differs');
const code = Cell.fromBase64(input.code);
const source = contractAddress(0, {code, data: Cell.fromBase64(input.sourceInit)});
const recipient = contractAddress(0, {code, data: Cell.fromBase64(input.recipientInit)});
assert(source.equals(Address.parse(input.source)), 'Canonical source jetton wallet differs');
assert(recipient.equals(Address.parse(input.recipient)), 'Canonical recipient jetton wallet differs');
await chain.setShardAccount(source, createShardAccount({
  address: source, code, data: Cell.fromBase64(input.sourceData), balance: toNano('0.2'),
}));
const funder = await chain.treasury('operator');
await funder.send({to: owner.address, value: toNano('10'), bounce: false});
const result = await chain.sendMessage(Cell.fromBase64(input.boc));
const destinationTx = result.transactions.find(tx => tx.address === BigInt('0x' + recipient.hash.toString('hex')));
assert(destinationTx && !destinationTx.description.aborted, 'Destination jetton transfer was not executed');
const readBalance = async address => (await chain.runGetMethod(address, 'get_wallet_data')).stackReader.readBigNumber();
assert.equal(await readBalance(source), BigInt(input.initial) - BigInt(input.amount));
assert.equal(await readBalance(recipient), BigInt(input.amount));
assert.equal(await chain.openContract(owner).getSeqno(), 1);
let replayRejected = false;
try { await chain.sendMessage(Cell.fromBase64(input.boc)); } catch { replayRejected = true; }
assert(replayRejected, 'Signed jetton transfer replay was accepted');
assert.equal(await readBalance(recipient), BigInt(input.amount));
console.log(JSON.stringify({jettonAmount: input.amount, destinationExecuted: true, replayRejected}));
