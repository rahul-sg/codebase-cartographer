#!/usr/bin/env python3
"""
Build a synthetic multi-service estate for testing.

Deliberately messy, in the ways real estates are messy: inconsistent service
naming, a shared database, a topic with no visible consumer, a service that
only exists as a dependency, generated code, a vendored directory, a huge file,
a binary file, a symlink loop, and a file with invalid UTF-8.
"""
import os
import shutil
import subprocess
import sys

def w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)

def git(repo, *args):
    subprocess.run(["git"] + list(args), cwd=repo, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def commit(repo, msg, author, date):
    git(repo, "add", "-A")
    env = dict(os.environ)
    env.update({"GIT_AUTHOR_NAME": author, "GIT_AUTHOR_EMAIL": author.lower().replace(" ", ".") + "@example.test",
                "GIT_COMMITTER_NAME": author, "GIT_COMMITTER_EMAIL": author.lower().replace(" ", ".") + "@example.test",
                "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=repo, check=True,
                   env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def build(root):
    if os.path.exists(root):
        shutil.rmtree(root)
    os.makedirs(root)

    # ================= order-svc (Java, Spring) =================
    o = os.path.join(root, "order-svc")
    w(o + "/pom.xml", "<project><artifactId>order-svc</artifactId></project>\n")
    w(o + "/src/main/java/com/acme/shop/order/OrderService.java", '''package com.acme.shop.order;

import com.acme.shop.order.client.PricingClient;
import org.springframework.kafka.core.KafkaTemplate;

/** Owns the order lifecycle. */
public class OrderService extends BaseService implements Auditable {
    private final PricingClient pricing;
    private final KafkaTemplate<String, String> kafkaTemplate;

    public Order submitOrder(OrderRequest req) {
        validateOrder(req);
        Price total = computeTotal(req);
        Order saved = persistOrder(req, total);
        kafkaTemplate.send("shop.order.submitted", saved.getId());
        return saved;
    }

    void validateOrder(OrderRequest req) {
        if (req == null) { throw new IllegalArgumentException("null"); }
        if (req.getLines().isEmpty()) { throw new IllegalArgumentException("empty"); }
    }

    private Order persistOrder(OrderRequest req, Price p) { return null; }

    public void cancelOrder(String id) {
        kafkaTemplate.send("shop.order.cancelled", id);
    }
}
''')
    w(o + "/src/main/java/com/acme/shop/order/client/PricingClient.java", '''package com.acme.shop.order.client;

import org.springframework.cloud.openfeign.FeignClient;

@FeignClient(name = "pricing-service", url = "${PRICING_SERVICE_URL}")
public interface PricingClient {
    Price computeTotal(OrderRequest req);
}
''')
    w(o + "/src/main/java/com/acme/shop/order/OrderController.java", '''package com.acme.shop.order;

import org.springframework.web.bind.annotation.*;

@RestController
public class OrderController {
    @PostMapping("/api/v1/orders")
    public Order create(@RequestBody OrderRequest req) { return null; }

    @GetMapping("/api/v1/orders/{id}")
    public Order get(@PathVariable String id) { return null; }

    @DeleteMapping("/api/v1/orders/{id}")
    public void cancel(@PathVariable String id) { }
}
''')
    w(o + "/src/main/resources/application.yml", '''spring:
  datasource:
    url: jdbc:postgresql://shop-db:5432/shop
PRICING_SERVICE_URL: http://pricing-svc:8080
CATALOG_SERVICE_HOST: catalog-svc
INVENTORY_API_URL: http://inventory-api.shop.svc.cluster.local:9090
NOTIFY_SERVICE_URL: http://notification-svc:8080
''')
    # generated code that must be ignored
    w(o + "/target/generated-sources/Gen.java", "public class Gen { public void x() {} }\n")
    w(o + "/src/main/java/com/acme/shop/order/generated/Stub.java", "public class Stub { public void y() {} }\n")

    # ================= pricing-svc (Java) =================
    p = os.path.join(root, "pricing-svc")
    w(p + "/pom.xml", "<project><artifactId>pricing-svc</artifactId></project>\n")
    w(p + "/src/main/java/com/acme/shop/pricing/PricingEngine.java", '''package com.acme.shop.pricing;

import org.springframework.kafka.annotation.KafkaListener;

public class PricingEngine {
    public Price computeTotal(OrderRequest req) {
        return applyDiscounts(basePrice(req));
    }
    private Price basePrice(OrderRequest r) { return null; }
    private Price applyDiscounts(Price p) { return p; }

    @KafkaListener(topics = "shop.order.submitted")
    public void onOrderSubmitted(String orderId) {
        recalculate(orderId);
    }

    void recalculate(String id) { }
}
''')
    w(p + "/src/main/resources/application.properties", '''spring.datasource.url=jdbc:postgresql://shop-db:5432/shop
catalog.service.url=http://catalog-svc:8080
''')

    # ================= catalog-svc (Python) =================
    c = os.path.join(root, "catalog-svc")
    w(c + "/pyproject.toml", "[project]\nname = 'catalog-svc'\n")
    w(c + "/catalog/api.py", '''"""Catalog HTTP surface."""
import os
import requests
from flask import Flask

app = Flask(__name__)
INVENTORY_API_URL = os.environ["INVENTORY_API_URL"]

class CatalogService:
    def lookup_product(self, sku):
        return self._fetch(sku)

    def _fetch(self, sku):
        return requests.get(INVENTORY_API_URL + "/stock/" + sku)

@app.route("/api/v1/products/<sku>")
def get_product(sku):
    return CatalogService().lookup_product(sku)

@app.route("/api/v1/products", methods=["POST"])
def create_product():
    return {}
''')
    w(c + "/catalog/consumer.py", '''from kafka import KafkaConsumer

def start():
    consumer = KafkaConsumer("shop.order.submitted")
    for msg in consumer:
        handle_order(msg)

def handle_order(msg):
    pass
''')

    # ================= inventory-api (Go) =================
    i = os.path.join(root, "inventory-api")
    w(i + "/go.mod", "module github.com/acme/inventory-api\n\ngo 1.21\n")
    w(i + "/main.go", '''package main

import "fmt"

type Stock struct {
    Sku string
}

func (s *Stock) Reserve(qty int) error {
    if qty <= 0 {
        return fmt.Errorf("bad qty")
    }
    return publishReserved(s.Sku)
}

func publishReserved(sku string) error {
    return producer.Publish("shop.inventory.reserved", sku)
}

func main() {}
''')
    w(i + "/config.yaml", '''database:
  url: mysql://inv-db:3306/inventory
order_service_url: http://order-svc:8080
''')

    # ================= ml-svc (Python, the AI surface) =================
    m = os.path.join(root, "ml-svc")
    w(m + "/requirements.txt", "scikit-learn\nnumpy\nanthropic\n")
    w(m + "/ml/matcher.py", '''"""Product matching model."""
from sklearn.base import BaseEstimator
import numpy as np

class ProductMatcher(BaseEstimator):
    """Matches partner SKUs to catalog products."""

    def fit(self, X, y):
        return self

    def predict(self, items):
        return [self.score_item(i) for i in items]

    def score_item(self, item):
        # TODO: this is a heuristic, replace with the trained model
        if item.get("exact"):
            return 1.0
        return 0.5
''')
    w(m + "/ml/llm.py", '''import os
import anthropic

PROMPT_TEMPLATE = """You are matching product descriptions.
Product A: {a}
Product B: {b}
Answer yes or no."""

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

def match_with_llm(a, b):
    return client.messages.create(
        model="claude-sonnet-5",
        messages=[{"role": "user", "content": PROMPT_TEMPLATE.format(a=a, b=b)}],
    )
''')
    w(m + "/ml/consumer.py", '''from kafka import KafkaConsumer

consumer = KafkaConsumer("shop.order.submitted")

def run():
    for m in consumer:
        enrich(m)

def enrich(msg):
    pass
''')

    # ================= shared infra =================
    w(root + "/docker-compose.yml", '''version: "3"
services:
  order-svc:
    image: acme/order-svc:latest
    depends_on:
      - pricing-svc
      - shop-db
  pricing-svc:
    image: acme/pricing-svc:1.4.2
  catalog-svc:
    image: acme/catalog-svc:latest
    depends_on:
      - inventory-api
  inventory-api:
    image: acme/inventory-api:latest
  ml-svc:
    image: acme/ml-svc:latest
''')
    w(root + "/openapi/order-svc.yaml", '''openapi: 3.0.0
info:
  title: Order Service
  version: 1.0.0
paths:
  /api/v1/orders:
    post:
      operationId: createOrder
      summary: Submit a new order
    get:
      operationId: listOrders
  /api/v1/orders/{id}:
    get:
      operationId: getOrder
    delete:
      operationId: cancelOrder
''')
    w(root + "/asyncapi/oms.yaml", '''asyncapi: 2.6.0
info:
  title: OMS Events
  version: 1.0.0
channels:
  shop.order.submitted:
    publish:
      operationId: publishOrderSubmitted
  shop.order.cancelled:
    subscribe:
      operationId: onOrderCancelled
''')

    # ---- nasty edge cases ----
    w(o + "/vendor/thirdparty/Huge.java", "public class Vendored { }\n")
    w(o + "/node_modules/pkg/index.js", "module.exports = function shouldNotAppear() {};\n")
    big = os.path.join(o, "src/main/java/com/acme/shop/order/Big.java")
    w(big, "public class Big {\n" + ("    // filler line\n" * 60000) + "}\n")
    with open(os.path.join(o, "src/main/resources/blob.bin"), "wb") as fh:
        fh.write(bytes(range(256)) * 500)
    with open(os.path.join(c, "catalog/badenc.py"), "wb") as fh:
        fh.write(b"# \xff\xfe invalid utf-8 \xc3\x28\nclass Weird:\n    def ok(self):\n        pass\n")
    w(c + "/catalog/empty.py", "")
    os.makedirs(os.path.join(root, "loop"), exist_ok=True)
    try:
        os.symlink(os.path.join(root, "loop"), os.path.join(root, "loop", "self"))
    except (OSError, NotImplementedError):
        pass

    # ================= git history =================
    authors = {"order-svc": ("Dana Reyes", "Sam Okafor"),
               "pricing-svc": ("Sam Okafor", "Dana Reyes"),
               "catalog-svc": ("Priya Nair", "Dana Reyes"),
               "inventory-api": ("Priya Nair", "Sam Okafor"),
               "ml-svc": ("Rahul Sengupta", "Priya Nair")}
    for name in ("order-svc", "pricing-svc", "catalog-svc", "inventory-api", "ml-svc"):
        repo = os.path.join(root, name)
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "t@t.test")
        git(repo, "config", "user.name", "t")
        a1, a2 = authors[name]
        commit(repo, "initial import", a1, "2026-01-15T10:00:00")

    def touch(repo, relpath, text):
        f = os.path.join(repo, relpath)
        os.makedirs(os.path.dirname(f), exist_ok=True)
        with open(f, "a") as fh:
            fh.write(text)

    osvc = os.path.join(root, "order-svc")
    psvc = os.path.join(root, "pricing-svc")
    csvc = os.path.join(root, "catalog-svc")
    isvc = os.path.join(root, "inventory-api")
    msvc = os.path.join(root, "ml-svc")

    OS_J = "src/main/java/com/acme/shop/order/OrderService.java"
    OC_J = "src/main/java/com/acme/shop/order/OrderController.java"
    PC_J = "src/main/java/com/acme/shop/order/client/PricingClient.java"
    PE_J = "src/main/java/com/acme/shop/pricing/PricingEngine.java"

    # 1. Strong in-repo coupling: the service and its controller always move
    #    together. A reviewer who knows this asks for both in one PR.
    for k in range(6):
        touch(osvc, OS_J, "// order change %d\n" % k)
        touch(osvc, OC_J, "// controller change %d\n" % k)
        commit(osvc, "TICKET-%d adjust order flow" % (2000 + k),
               "Dana Reyes" if k % 2 else "Sam Okafor",
               "2026-0%d-0%dT09:00:00" % (2 + k % 6, 1 + k))

    # 2. Cross-repo coupling via a shared ticket key: the contract between
    #    order-svc and pricing-svc changes on both sides at once. No static
    #    analysis can see this; only the history shows it.
    for k in range(5):
        ticket = "TICKET-%d" % (3100 + k)
        touch(osvc, PC_J, "// pricing contract %d\n" % k)
        commit(osvc, "%s widen pricing contract" % ticket, "Dana Reyes",
               "2026-0%d-1%dT11:00:00" % (3 + k % 5, 2 + k))
        touch(psvc, PE_J, "// engine contract %d\n" % k)
        commit(psvc, "%s widen pricing contract" % ticket, "Sam Okafor",
               "2026-0%d-1%dT11:30:00" % (3 + k % 5, 2 + k))

    # 3. A hotspot: high churn, high complexity, one author (bus factor 1).
    for k in range(9):
        touch(csvc, "catalog/api.py",
              "\n\ndef branchy_%d(x):\n    if x > 1:\n        return 1\n    elif x < 0:\n        return 2\n    return 3\n" % k)
        commit(csvc, "TICKET-%d catalog tweak" % (4000 + k), "Priya Nair",
               "2026-0%d-2%dT14:00:00" % (2 + k % 7, k % 9))

    # 4. Ordinary background churn elsewhere.
    for k in range(4):
        touch(isvc, "main.go", "// inv %d\n" % k)
        commit(isvc, "inventory tweak %d" % k, "Priya Nair" if k % 2 else "Sam Okafor",
               "2026-0%d-0%dT16:00:00" % (2 + k, 5 + k))
    for k in range(3):
        touch(msvc, "ml/matcher.py", "\n# ml %d\n" % k)
        commit(msvc, "TICKET-%d matcher tuning" % (5000 + k), "Rahul Sengupta",
               "2026-0%d-1%dT10:00:00" % (4 + k, 4 + k))
    return root


if __name__ == "__main__":
    dest = sys.argv[1] if len(sys.argv) > 1 else "/tmp/cart-fixture"
    print(build(os.path.abspath(dest)))
