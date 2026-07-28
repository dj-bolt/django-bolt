#!/usr/bin/env python3
"""
WebSocket Load Test Script
Measures real WebSocket performance with concurrent clients.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime
from statistics import mean, stdev

import psutil
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException


class WebSocketLoadTest:
    def __init__(
        self,
        url: str,
        num_clients: int = 10,
        duration: int = 30,
        batch_size: int = 10,
        message_interval: float = 0.1,
    ):
        self.url = url
        self.num_clients = num_clients
        self.duration = duration
        self.batch_size = batch_size
        self.message_interval = message_interval
        self.results = []
        self.start_time = None
        self.process = psutil.Process(os.getpid())
        self.cpu_samples = []  # Track CPU samples during test

    async def ws_client(self, client_id: int) -> dict:
        """Simulate one WebSocket client sending and receiving messages"""
        client_start = time.time()
        messages_sent = 0
        messages_received = 0
        bytes_sent = 0
        bytes_received = 0
        latencies = []
        error = None

        try:
            async with websockets.connect(self.url) as ws:
                # Send and receive messages until duration exceeded
                while True:
                    elapsed = time.time() - client_start
                    if elapsed > self.duration:
                        break

                    try:
                        # Send a message with timestamp
                        send_time = time.time()
                        message = f"ping:{client_id}:{send_time}"
                        await ws.send(message)
                        messages_sent += 1
                        bytes_sent += len(message)

                        # Wait for response with timeout
                        try:
                            response = await asyncio.wait_for(ws.recv(), timeout=5.0)
                            recv_time = time.time()
                            messages_received += 1
                            bytes_received += len(response)

                            # Calculate round-trip latency
                            latency = (recv_time - send_time) * 1000  # ms
                            latencies.append(latency)

                        except TimeoutError:
                            pass  # No response, continue

                        # Wait before next message
                        await asyncio.sleep(self.message_interval)

                    except ConnectionClosed:
                        break

        except TimeoutError:
            error = "Connection timeout"
        except WebSocketException as e:
            error = f"WebSocket error: {str(e)[:50]}"
        except OSError as e:
            error = f"Connection error: {str(e)[:50]}"
        except Exception as e:
            error = f"Error: {str(e)[:50]}"

        elapsed = time.time() - client_start
        status = "failed" if error else "success"

        avg_latency = mean(latencies) if latencies else 0
        print(
            f"    Client {client_id}: {status} "
            f"(sent:{messages_sent}, recv:{messages_received}, "
            f"lat:{avg_latency:.1f}ms, {elapsed:.1f}s)"
            f"{f' - {error}' if error else ''}",
            flush=True,
        )

        return {
            "client_id": client_id,
            "status": "success" if not error else "failed",
            "error": error,
            "messages_sent": messages_sent,
            "messages_received": messages_received,
            "bytes_sent": bytes_sent,
            "bytes_received": bytes_received,
            "latencies": latencies,
            "duration": elapsed,
        }

    def get_system_stats(self) -> dict:
        """Get current process resource usage"""
        try:
            mem_info = self.process.memory_info()
            return {
                "memory_mb": mem_info.rss / 1024 / 1024,
                "cpu_percent": self.process.cpu_percent(interval=0),
            }
        except Exception:
            return {"memory_mb": 0, "cpu_percent": 0}

    async def _sample_cpu_during_batch(self, duration: float) -> None:
        """Sample CPU usage every 0.1s for the given duration"""
        start = time.time()
        while time.time() - start < duration:
            self.cpu_samples.append(self.process.cpu_percent(interval=0))
            await asyncio.sleep(0.1)

    async def run(self) -> None:
        """Run the load test"""
        print(f"\n{'=' * 70}")
        print("WebSocket Load Test")
        print(f"{'=' * 70}")
        print(f"URL: {self.url}")
        print(f"Concurrent Clients: {self.num_clients}")
        print(f"Duration per Client: {self.duration}s")
        print(f"Message Interval: {self.message_interval}s")
        print(f"Start Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'=' * 70}\n")

        self.start_time = time.time()

        # Print progress
        print("Launching clients...\n")
        for i in range(0, self.num_clients, self.batch_size):
            batch_size = min(self.batch_size, self.num_clients - i)
            print(f"  [{i:4d}/{self.num_clients}] Launching {batch_size} clients...")

            # Launch in batches to avoid overwhelming the system
            tasks = [self.ws_client(j) for j in range(i, i + batch_size)]
            print(
                f"  Waiting for {len(tasks)} clients to run for {self.duration}s...",
                flush=True,
            )

            # Run batch tasks and CPU sampling concurrently
            batch_tasks = asyncio.gather(*tasks)
            cpu_sampler = self._sample_cpu_during_batch(self.duration + 5)

            batch_results, _ = await asyncio.gather(batch_tasks, cpu_sampler)
            self.results.extend(batch_results)

            elapsed = time.time() - self.start_time
            sys_stats = self.get_system_stats()
            print(
                f"  Batch complete ({elapsed:.1f}s, {sys_stats['memory_mb']:.1f}MB, "
                f"CPU: {sys_stats['cpu_percent']:.1f}%)\n"
            )

        total_time = time.time() - self.start_time
        print(f"\nTest completed in {total_time:.2f}s")
        print()

        # Print detailed results
        self._print_results()

    def _print_results(self) -> None:
        """Print and analyze results"""
        successful = [r for r in self.results if r["status"] == "success"]
        failed = [r for r in self.results if r["status"] == "failed"]

        print(f"{'=' * 70}")
        print("RESULTS")
        print(f"{'=' * 70}\n")

        # Summary
        print(f"Total Clients: {self.num_clients}")
        print(f"Successful: {len(successful)} ({len(successful) / self.num_clients * 100:.1f}%)")
        print(f"Failed: {len(failed)} ({len(failed) / self.num_clients * 100:.1f}%)")

        if failed:
            print("\nFailure Details:")
            for r in failed[:5]:  # Show first 5 failures
                print(f"  Client {r['client_id']}: {r['error']}")
            if len(failed) > 5:
                print(f"  ... and {len(failed) - 5} more failures")

        print()

        if successful:
            # Message stats
            sent_list = [r["messages_sent"] for r in successful]
            recv_list = [r["messages_received"] for r in successful]
            bytes_sent_list = [r["bytes_sent"] for r in successful]
            bytes_recv_list = [r["bytes_received"] for r in successful]
            duration_list = [r["duration"] for r in successful]

            # Flatten all latencies
            all_latencies = []
            for r in successful:
                all_latencies.extend(r["latencies"])

            print("Message Delivery:")
            print(f"  Total Sent: {sum(sent_list):,}")
            print(f"  Total Received: {sum(recv_list):,}")
            print(f"  Avg Sent/Client: {mean(sent_list):.1f}")
            print(f"  Avg Received/Client: {mean(recv_list):.1f}")
            if len(sent_list) > 1:
                print(f"  StDev Sent: {stdev(sent_list):.1f}")

            print("\nData Transfer:")
            total_bytes = sum(bytes_sent_list) + sum(bytes_recv_list)
            print(f"  Total Bytes: {total_bytes:,} ({total_bytes / 1024 / 1024:.2f} MB)")
            print(f"  Bytes Sent: {sum(bytes_sent_list):,}")
            print(f"  Bytes Received: {sum(bytes_recv_list):,}")
            avg_duration = mean(duration_list)
            if avg_duration > 0:
                print(f"  Throughput: {total_bytes / avg_duration / 1024:.2f} KB/s")

            if all_latencies:
                print("\nLatency (round-trip):")
                print(f"  Avg: {mean(all_latencies):.2f} ms")
                if len(all_latencies) > 1:
                    print(f"  StDev: {stdev(all_latencies):.2f} ms")
                print(f"  Min: {min(all_latencies):.2f} ms")
                print(f"  Max: {max(all_latencies):.2f} ms")
                # P50, P95, P99
                sorted_lat = sorted(all_latencies)
                p50 = sorted_lat[int(len(sorted_lat) * 0.50)]
                p95 = sorted_lat[int(len(sorted_lat) * 0.95)]
                p99 = sorted_lat[int(len(sorted_lat) * 0.99)]
                print(f"  P50: {p50:.2f} ms")
                print(f"  P95: {p95:.2f} ms")
                print(f"  P99: {p99:.2f} ms")

            print("\nConnection Duration:")
            print(f"  Avg Duration: {mean(duration_list):.2f}s")
            if len(duration_list) > 1:
                print(f"  StDev: {stdev(duration_list):.2f}s")
            print(f"  Min/Max: {min(duration_list):.2f}s / {max(duration_list):.2f}s")

            print("\nMessaging Rate:")
            avg_rate = mean([s / d for s, d in zip(sent_list, duration_list, strict=True)])
            print(f"  Avg Messages/sec/client: {avg_rate:.2f}")
            print(f"  Total Messages/sec: {sum(sent_list) / mean(duration_list):.2f}")

        print()

        # Resource usage
        sys_stats = self.get_system_stats()
        avg_cpu = mean(self.cpu_samples) if self.cpu_samples else 0
        print("Resource Usage:")
        print(f"  Memory: {sys_stats['memory_mb']:.1f} MB")
        print(f"  CPU (current): {sys_stats['cpu_percent']:.1f}%")
        if self.cpu_samples:
            print(f"  CPU (avg during test): {avg_cpu:.1f}%")
            print(f"  CPU (min/max during test): {min(self.cpu_samples):.1f}% / {max(self.cpu_samples):.1f}%")

        print()
        print(f"{'=' * 70}\n")


async def main() -> None:
    """Main entry point"""
    parser = argparse.ArgumentParser(description="WebSocket Load Test - Measures concurrent WebSocket performance")
    parser.add_argument(
        "url",
        nargs="?",
        default="ws://127.0.0.1:8000/ws",
        help="URL of WebSocket endpoint (default: ws://127.0.0.1:8000/ws)",
    )
    parser.add_argument(
        "-c",
        "--clients",
        type=int,
        default=50,
        help="Number of concurrent clients (default: 50)",
    )
    parser.add_argument(
        "-d",
        "--duration",
        type=int,
        default=30,
        help="Duration per client in seconds (default: 30)",
    )
    parser.add_argument(
        "-b",
        "--batch-size",
        type=int,
        default=-1,
        help="Launch batch size (default: -1, use -1 for all at once)",
    )
    parser.add_argument(
        "-i",
        "--interval",
        type=float,
        default=0.1,
        help="Message interval in seconds (default: 0.1)",
    )

    args = parser.parse_args()

    # If batch_size is -1, launch all at once
    if args.batch_size == -1:
        args.batch_size = args.clients

    # Validate URL
    if not args.url.startswith("ws"):
        print(f"Error: Invalid URL '{args.url}'. Must start with ws:// or wss://")
        sys.exit(1)

    # Run test
    test = WebSocketLoadTest(
        args.url,
        num_clients=args.clients,
        duration=args.duration,
        batch_size=args.batch_size,
        message_interval=args.interval,
    )
    await test.run()


if __name__ == "__main__":
    asyncio.run(main())
