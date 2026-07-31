const assert = require('node:assert/strict');

const { ReconnectController } = require('../web/js/terminal-reconnect.js');

class FakeTimers {
    constructor() {
        this.nextId = 1;
        this.queue = new Map();
    }

    setTimeout(callback, delay) {
        const id = this.nextId++;
        this.queue.set(id, { callback, delay });
        return id;
    }

    clearTimeout(id) {
        this.queue.delete(id);
    }

    nextDelay() {
        return this.queue.values().next().value?.delay;
    }

    async runNext() {
        const item = this.queue.values().next().value;
        assert.ok(item, 'expected a scheduled retry');
        const id = [...this.queue.entries()].find(([, value]) => value === item)[0];
        this.queue.delete(id);
        await item.callback();
    }
}

async function testRetriesWithBackoffUntilConnectionSucceeds() {
    const timers = new FakeTimers();
    const attempts = [];
    const controller = new ReconnectController(
        (attempt) => {
            attempts.push(attempt);
            return attempts.length === 2;
        },
        {
            delays: [250, 500, 1000],
            setTimeoutFn: timers.setTimeout.bind(timers),
            clearTimeoutFn: timers.clearTimeout.bind(timers),
        },
    );

    assert.equal(controller.schedule(), true);
    assert.equal(timers.nextDelay(), 250);

    await timers.runNext();
    assert.deepEqual(attempts, [1]);
    assert.equal(timers.nextDelay(), 500);

    await timers.runNext();
    assert.deepEqual(attempts, [1, 2]);
    assert.equal(controller.pending, false);
    assert.equal(controller.attempt, 0);
}

async function testCancelPreventsAQueuedReconnect() {
    const timers = new FakeTimers();
    let attempts = 0;
    const controller = new ReconnectController(
        () => {
            attempts += 1;
            return true;
        },
        {
            setTimeoutFn: timers.setTimeout.bind(timers),
            clearTimeoutFn: timers.clearTimeout.bind(timers),
        },
    );

    controller.schedule();
    controller.cancel();

    assert.equal(controller.pending, false);
    assert.equal(timers.nextDelay(), undefined);
    assert.equal(attempts, 0);
}

Promise.resolve()
    .then(testRetriesWithBackoffUntilConnectionSucceeds)
    .then(testCancelPreventsAQueuedReconnect)
    .then(() => console.log('terminal-reconnect tests passed'));
