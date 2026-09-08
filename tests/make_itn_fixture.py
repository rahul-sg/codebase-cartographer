#!/usr/bin/env python3
"""
A fixture shaped like the real iTradeNetwork OMS workspace.

Mirrors the conventions that matter, from the stack reference:
  * 5 sibling repos under one non-repo workspace root
  * Maven multi-module backend; two services deliberately OUTSIDE the reactor
  * hand-written JdbcTemplate DAOs, no JPA anywhere
  * Kafka topics as enum CONSTANTS resolved at runtime, not string literals
  * Angular proxy.config.json as the frontend -> backend edge source
  * legacy patch-list SQL and Flyway V*.sql side by side
  * two modules sharing one schema
  * a credential-shaped string that must be flagged and never stored
"""
import os
import shutil
import subprocess
import sys


def w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def build(root):
    if os.path.exists(root):
        shutil.rmtree(root)
    os.makedirs(root)

    S = os.path.join(root, "ong-server-repo")
    srv = os.path.join(S, "server")

    # ---------------- parent pom: reactor list omits logistics + interop ----
    reactor = ["gcutil", "cache", "kafkautil", "framework", "auth", "misc",
               "common", "catalog", "company", "order", "comment",
               "notification", "nexus", "agent", "order-enterprise"]
    w(srv + "/pom.xml", """<?xml version="1.0"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <groupId>com.itradenetwork</groupId>
  <artifactId>omm</artifactId>
  <version>0.0.1</version>
  <packaging>pom</packaging>
  <properties><java.version>21</java.version></properties>
  <modules>
%s
  </modules>
</project>
""" % "\n".join("    <module>%s</module>" % m for m in reactor))

    def module_pom(name, deps=(), standalone=False):
        dep_xml = "\n".join(
            "    <dependency><groupId>com.itradenetwork</groupId>"
            "<artifactId>%s</artifactId><version>0.0.1</version></dependency>" % d
            for d in deps)
        parent = "" if standalone else """  <parent>
    <groupId>com.itradenetwork</groupId><artifactId>omm</artifactId>
    <version>0.0.1</version>
  </parent>
"""
        gid = "  <groupId>com.itradenetwork</groupId>\n" if standalone else ""
        return """<?xml version="1.0"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
%s%s  <artifactId>%s</artifactId>
  <version>0.0.1</version>
  <dependencies>
%s
  </dependencies>
</project>
""" % (parent, gid, name, dep_xml)

    libs = {"gcutil": (), "cache": (), "kafkautil": (), "auth": ("cache",),
            "framework": ("cache", "gcutil"), "misc": ("kafkautil",)}
    services = {
        "common":           ("cmndev", ("auth", "framework", "cache", "kafkautil", "gcutil", "misc")),
        "company":          ("cmnydev", ("framework", "cache", "kafkautil", "misc")),
        "order":            ("orddev", ("framework", "cache", "kafkautil", "common")),
        "catalog":          ("ctlgdev", ("framework", "cache", "kafkautil")),
        "comment":          ("cmtdev", ("framework", "cache")),
        "notification":     ("notifdev", ("framework", "kafkautil")),
        "nexus":            ("cmndev", ("framework", "cache")),   # shares common's schema
        "agent":            ("emailagentdev", ("framework", "cache")),
        "order-enterprise": ("omedev", ("framework", "kafkautil")),
    }
    outside = {"logistics": "logdev", "interoperability": "intopdev"}

    for lib, deps in libs.items():
        w("%s/%s/pom.xml" % (srv, lib), module_pom(lib, deps))
    for svc, (schema, deps) in services.items():
        w("%s/%s/pom.xml" % (srv, svc), module_pom(svc, deps))
        # every deployable module ships a container and its own Spring config
        w("%s/%s/Dockerfile" % (srv, svc), "FROM eclipse-temurin:21\n")
        if not os.path.exists("%s/%s/src/main/resources/application.properties" % (srv, svc)):
            w("%s/%s/src/main/resources/application.properties" % (srv, svc),
              "spring.application.name=%s\ndb_uri=jdbc:mysql://mysqldb:3306/%s\n"
              % (svc, schema))
    for svc in outside:
        # logistics: written like a child but excluded from <modules>
        w("%s/%s/pom.xml" % (srv, svc),
          module_pom(svc, ("framework",), standalone=(svc == "interoperability")))
        w("%s/%s/Dockerfile" % (srv, svc), "FROM eclipse-temurin:21\n")
        w("%s/%s/deployHelm/values.yaml" % (srv, svc), "replicaCount: 2\n")

    # ---------------- Kafka constants: the enum, not string literals -------
    w(srv + "/misc/src/main/java/com/itradenetwork/misc/kafka/KafkaConstants.java",
      """package com.itradenetwork.misc.kafka;

public class KafkaConstants {
    public enum KafkaTopicName {
        ENTITY_EVENT_CREATE("entity.event.create"),
        ENTITY_ALERT("entity.alert"),
        ENTITY_VENDOR_MM("entity.vendor.mm"),
        ENTITY_LOCATION_MM("entity.location.mm"),
        ENTITY_COMPANY_INITIAL_SETUP("entity.company.initial.setup"),
        ORDER_SUBMITTED("order.submitted");

        private final String topic;
        KafkaTopicName(String topic) { this.topic = topic; }
        public String getTopic() { return topic; }
    }
}
""")
    w(srv + "/kafkautil/src/main/java/com/itradenetwork/kafkautil/util/TopicNameCreator.java",
      """package com.itradenetwork.kafkautil.util;

public class TopicNameCreator {
    public String createTopicName(Object name) { return env + "." + name; }
}
""")

    # producers / listeners referencing the enum constants
    w(srv + "/order/src/main/java/com/itradenetwork/order/OrderService.java",
      """package com.itradenetwork.order;

import com.itradenetwork.misc.kafka.KafkaConstants;
import com.itradenetwork.kafkautil.util.TopicNameCreator;
import org.springframework.jdbc.core.JdbcTemplate;

public class OrderService {
    private final TopicNameCreator topicNameCreator;
    private final JdbcTemplate jdbcTemplate;

    public Order submitOrder(OrderRequest req) {
        validateOrder(req);
        kafkaTemplate.send(
            topicNameCreator.createTopicName(KafkaConstants.KafkaTopicName.ORDER_SUBMITTED),
            req.getId());
        return persist(req);
    }

    void validateOrder(OrderRequest req) {
        if (req == null) { throw new IllegalArgumentException(); }
    }

    private Order persist(OrderRequest req) {
        return jdbcTemplate.queryForObject(
            "SELECT po_id, po_number, buyer_id FROM T_PURCHASE_ORDER WHERE po_id = ?",
            new OrderRowMapper(), req.getId());
    }
}
""")
    w(srv + "/notification/src/main/java/com/itradenetwork/notification/NotificationListener.java",
      """package com.itradenetwork.notification;

import com.itradenetwork.misc.kafka.KafkaConstants;
import org.springframework.kafka.annotation.KafkaListener;

public class NotificationListener {
    @KafkaListener(topics = "#{topicNameCreator.createTopicName(T(com.itradenetwork.misc.kafka.KafkaConstants.KafkaTopicName).ORDER_SUBMITTED)}")
    public void onOrderSubmitted(String id) { send(id); }

    void send(String id) { }
}
""")
    w(srv + "/company/src/main/java/com/itradenetwork/company/LocationListener.java",
      """package com.itradenetwork.company;

import com.itradenetwork.misc.kafka.KafkaConstants.KafkaTopicName;
import org.springframework.kafka.annotation.KafkaListener;

public class LocationListener {
    @KafkaListener(topics = "${kafka.topic:" + "}")
    public void onLocation(String msg) { }

    public void publish() {
        kafkaTemplate.send(topicNameCreator.createTopicName(KafkaTopicName.ENTITY_LOCATION_MM), "x");
    }
}
""")

    # ---------------- Spring controllers: class-level + method-level -------
    w(srv + "/order/src/main/java/com/itradenetwork/order/OrderController.java",
      """package com.itradenetwork.order;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/order/api/v1/purchase-orders")
public class OrderController {

    @PostMapping
    public Order create(@RequestBody OrderRequest req) { return null; }

    @GetMapping("/{id}")
    public Order get(@PathVariable String id) { return null; }

    @DeleteMapping("/{id}")
    public void cancel(@PathVariable String id) { }
}
""")
    w(srv + "/catalog/src/main/java/com/itradenetwork/catalog/CatalogController.java",
      """package com.itradenetwork.catalog;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping(value = "/catalog/api/products")
public class CatalogController {
    @GetMapping("/{sku}")
    public Product get(@PathVariable String sku) { return null; }
}
""")

    # ---------------- DAOs: the three-file convention, raw SQL -------------
    w(srv + "/catalog/src/main/java/com/itradenetwork/catalog/dao/ProductDaoImpl.java",
      """package com.itradenetwork.catalog.dao;

import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.cache.annotation.Cacheable;
import com.itradenetwork.cache.annotation.RedisCacheable;

public class ProductDaoImpl implements ProductDao {
    private NamedParameterJdbcTemplate namedParameterJdbcTemplate;

    @RedisCacheable(key = "product")
    public Product findBySku(String sku) {
        String sql = "SELECT p.product_id, p.sku, p.name FROM T_PRODUCT p "
                   + "JOIN T_PRODUCT_PRICE pp ON pp.product_id = p.product_id "
                   + "WHERE p.sku = :sku";
        return namedParameterJdbcTemplate.queryForObject(sql, params, new ProductRowMapper());
    }

    public void updatePrice(long id, double price) {
        namedParameterJdbcTemplate.update(
            "UPDATE T_PRODUCT_PRICE SET price = :price WHERE product_id = :id", params);
    }
}
""")
    w(srv + "/catalog/src/main/java/com/itradenetwork/catalog/dao/ProductRowMapper.java",
      "package com.itradenetwork.catalog.dao;\npublic class ProductRowMapper { }\n")
    w(srv + "/catalog/src/main/java/com/itradenetwork/catalog/dao/ProductDao.java",
      "package com.itradenetwork.catalog.dao;\npublic interface ProductDao { }\n")
    # a second service reading the SAME table -> hidden data coupling
    w(srv + "/order/src/main/java/com/itradenetwork/order/dao/OrderProductDaoImpl.java",
      """package com.itradenetwork.order.dao;

import org.springframework.jdbc.core.JdbcTemplate;

public class OrderProductDaoImpl {
    private JdbcTemplate jdbcTemplate;

    public List<Product> lookup(long id) {
        return jdbcTemplate.query(
            "SELECT sku, name FROM T_PRODUCT WHERE product_id = ?",
            new ProductRowMapper(), id);
    }

    public void writeAudit() {
        jdbcTemplate.update("INSERT INTO T_ORDER_AUDIT (po_id, action) VALUES (?, ?)");
    }
}
""")

    # ---------------- migrations: both systems -----------------------------
    w(S + "/database/order/patches/patchlist-ddl.txt",
      "all_ong_12345_create_purchase_order.sql\nall_ong_12346_add_audit.sql\n")
    w(S + "/database/order/patches/all_ong_12345_create_purchase_order.sql",
      """CREATE TABLE T_PURCHASE_ORDER (
  po_id BIGINT NOT NULL AUTO_INCREMENT,
  po_number VARCHAR(50),
  buyer_id BIGINT,
  status VARCHAR(20),
  PRIMARY KEY (po_id)
);
""")
    w(S + "/database/order/patches/all_ong_12346_add_audit.sql",
      "CREATE TABLE T_ORDER_AUDIT (\n  audit_id BIGINT,\n  po_id BIGINT,\n  action VARCHAR(40)\n);\n"
      "ALTER TABLE T_PURCHASE_ORDER ADD COLUMN cancelled_at DATETIME;\n")
    w(srv + "/catalog/src/main/resources/db/migration/all/V1628239274__ddl_create_T_PRODUCT.sql",
      "CREATE TABLE T_PRODUCT (\n  product_id BIGINT,\n  sku VARCHAR(64),\n  name VARCHAR(255)\n);\n")
    w(srv + "/catalog/src/main/resources/db/migration/all/V1628239300__ddl_create_price.sql",
      "CREATE TABLE IF NOT EXISTS T_PRODUCT_PRICE (\n  product_id BIGINT,\n  price DECIMAL(10,2)\n);\n")

    # ---------------- properties incl. a credential-shaped value -----------
    w(srv + "/agent/src/main/resources/application.properties",
      """spring.application.name=agent
db_uri=jdbc:mysql://mysqlLocal3307:3306/emailagentdev
oracle.enable=false
redis.cache.enable=true
kafka.bootstrap.servers=pkc-abcde.us-central1.gcp.confluent.cloud:9092
kafka.sasl.jaas.config=org.apache.kafka.common.security.plain.PlainLoginModule required username="AAAAXXXXBBBBCCCC" password="s3cr3tValueThatMustNeverBeStored/AbCdEf+12345";
openai.api.key=sk-proj-NOTAREALKEYbutShapedLikeOne1234567890
""")
    w(srv + "/order/src/main/resources/application.properties",
      "spring.application.name=order\ndb_uri=jdbc:mysql://mysqldb:3306/orddev\n"
      "oracle.enable=true\noracle.jndi=java:/jdbc/tpcom\n")

    # ---------------- devops ----------------------------------------------
    D = os.path.join(root, "ong-devops")
    w(D + "/local/docker-compose.yml", """version: "3"
services:
  mysqldb:
    image: gcr.io/dev-sqe-uat/ong_mysql_db
    ports: ["3306:3306"]
  redis:
    image: redis
  kafka:
    image: confluentinc/cp-kafka
  common:
    image: gcr.io/dev-sqe-uat/common
    environment:
      - db_uri=jdbc:mysql://mysqldb:3306/cmndev
      - db_username=root
  nexus:
    image: gcr.io/dev-sqe-uat/nexus
    environment:
      - db_uri=jdbc:mysql://mysqldb:3306/cmndev
  order:
    image: gcr.io/dev-sqe-uat/order
    environment:
      - db_uri=jdbc:mysql://mysqldb:3306/orddev
    depends_on: [mysqldb, kafka, common]
  catalog:
    image: gcr.io/dev-sqe-uat/catalog
    environment:
      - db_uri=jdbc:mysql://mysqldb:3306/ctlgdev
  ome:
    image: gcr.io/dev-sqe-uat/order-enterprise
    environment:
      - db_uri=jdbc:mysql://mysqldb:3306/omedev
""")

    # ---------------- frontends -------------------------------------------
    U = os.path.join(root, "ong-ui-repo")
    w(U + "/ui/package.json",
      '{"name":"omsnextgen-ui","version":"19.26.150",'
      '"dependencies":{"@angular/core":"19.2.14","@itn/itn-library2":"19.2609.1","@ngrx/store":"19.0.0"}}')
    w(U + "/ui/proxy.config.json", """{
  "/common/": { "target": "https://ongsqe.itradenetwork.net", "secure": false },
  "/order/":  { "target": "https://ongsqe.itradenetwork.net", "secure": false },
  "/catalog/": { "target": "https://ongsqe.itradenetwork.net" },
  "/company/": { "target": "https://ongsqe.itradenetwork.net" },
  "/comment/": { "target": "https://ongsqe.itradenetwork.net" },
  "/notification/": { "target": "https://ongsqe.itradenetwork.net" },
  "/omsenterprise/": { "target": "https://ongsqe.itradenetwork.net" },
  "/agent/": { "target": "https://ongsqe.itradenetwork.net" },
  "/logistics/": { "target": "https://itlsqe.itradenetwork.net" },
  "/contract/v1/": { "target": "https://icrsqe.itradenetwork.net" },
  "/interoperability/": { "target": "https://ongsqe.itradenetwork.net" },
  "/rfq-agent/": { "target": "https://imlsqe.itradenetwork.net" }
}""")
    w(U + "/ui/src/app/core/utils/sso.util.ts", """
export const LOGON = 'secure/login/logon.cfm';
export const CHECK = 'login/check_logon.cfm';
export function extend() { return 'extendSession.cfm'; }
""")
    w(U + "/ui/src/app/common/services/common.service.ts", """
import { HttpClient } from '@angular/common/http';

export class CommonService {
  poDetails(id: string) { return this.http.get('/order/api/v1/purchase-orders/' + id); }
  enterprise(id: string) { return this.enterpriseURL + 'buy/po_details.cfm?id=' + id; }
  product(sku: string) { return this.http.get(`/catalog/api/products/${sku}`); }
}
""")

    A = os.path.join(root, "om-angular-repo")
    w(A + "/OrderAndStock/package.json",
      '{"name":"order-and-stoc","version":"0.0.1",'
      '"dependencies":{"@angular/core":"16.2.12","@itn/itn-library2":"16.24.527"}}')
    w(A + "/OrderAndStock/proxy.config.json", """{
  "/common/": { "target": "https://iomsqe.itradenetwork.net" },
  "/order/": { "target": "https://iomsqe.itradenetwork.net" },
  "/catalog/": { "target": "https://iomsqe.itradenetwork.net" },
  "/inventory/": { "target": "https://iomsqe.itradenetwork.net" }
}""")

    R = os.path.join(root, "bp-react-repo")
    w(R + "/OrderAndStock/package.json",
      '{"name":"OrderAndStock","version":"0.0.1",'
      '"dependencies":{"react-native":"0.80.0","react":"19.1.0"}}')
    w(R + "/OrderAndStock/src/utils/url.js", """
export const DEV_BASE_URL = 'https://ongsqe.itradenetwork.net';
export const PROD_BASE_URL = 'https://www.itradeorder.com';
export const QMS_BASE_URL = 'https://qmssqe.itradenetwork.net';
export const ANDROID_EMULATOR = 'http://10.0.2.2:9480';
""")

    for repo in ("ong-server-repo", "ong-ui-repo", "om-angular-repo",
                 "bp-react-repo", "ong-devops"):
        d = os.path.join(root, repo)
        subprocess.run(["git", "init", "-q"], cwd=d)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=d)
        subprocess.run(["git", "config", "user.name", "t"], cwd=d)
        subprocess.run(["git", "add", "-A"], cwd=d, stdout=subprocess.DEVNULL)
        env = dict(os.environ, GIT_AUTHOR_NAME="Dana Reyes",
                   GIT_AUTHOR_EMAIL="d@itn.test", GIT_COMMITTER_NAME="Dana Reyes",
                   GIT_COMMITTER_EMAIL="d@itn.test",
                   GIT_AUTHOR_DATE="2026-03-01T10:00:00",
                   GIT_COMMITTER_DATE="2026-03-01T10:00:00")
        subprocess.run(["git", "commit", "-qm", "OMSR-1000 initial"], cwd=d,
                       env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return root


if __name__ == "__main__":
    print(build(os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "/tmp/itn")))
