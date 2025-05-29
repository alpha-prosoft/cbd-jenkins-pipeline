(ns build
  (:require [clojure.tools.build.api :as b]
            [clojure.java.io :as io]
            [clojure.edn :as edn])
  (:import [java.util.zip ZipFile]))

(defn build-opts
  [{:keys [group-id artifact-id version]}]
  (assert (and group-id (seq (name group-id)))
          "Mandatory :group-id missing or empty.")
  (assert (and artifact-id (seq (name artifact-id)))
          "Mandatory :artifact-id missing or empty.")
  (assert (and version (seq version))
          "Mandatory :version missing or empty.")
  {:group-id group-id
   :artifact-id artifact-id
   :lib (symbol (name group-id) (name artifact-id))
   :version version
   :class-dir "target/classes"
   :basis (b/create-basis {:project "deps.edn"})
   :jar-file (format "target/%s-%s.jar" (name artifact-id) version)
   :uber-file (format "target/%s-%s-standalone.jar" (name artifact-id) version)
   :src-dirs ["src"]
   :resource-dirs ["resources"]})

(defn clean
  "Clean the target directory."
  [opts]
  (let [{:keys [class-dir]} (merge (build-opts opts) opts)]
    (println "Cleaning target directory...")
    (b/delete {:path (-> class-dir io/file .getParent)}))) ; Delete the parent of class-dir ("target")

(defn jar
  "Build the JAR file."
  [opts]
  (let [{:keys [class-dir
                lib
                version
                basis
                src-dirs
                resource-dirs
                jar-file] :as merged-opts}
        (merge (build-opts opts) opts)]

    (println "Building JAR:" jar-file)
    (clean merged-opts)
    (println "Writing pom file")
    (b/write-pom {:class-dir class-dir
                  :lib lib
                  :version version
                  :basis basis
                  :src-dirs src-dirs})
    (b/copy-dir {:src-dirs (concat src-dirs resource-dirs) ; Combine src and resource dirs
                 :target-dir class-dir})


    (println "Building jar")
    (b/jar {:class-dir class-dir
            :jar-file jar-file})))

(defn- find-compile-ns-in-jar
  "Attempts to read META-INF/build.edn from a JAR file and returns the value of :ns-compile."
  [jar-path]
  (println (format "    Searching build.edn: %s", jar-path))
  (try
    (with-open [zip (ZipFile. (io/file jar-path))]
      (when-let [entry (.getEntry zip "META-INF/build.edn")]
        (with-open [is (.getInputStream zip entry)]
          (let [content 
                (slurp is)
                
                data
                (edn/read-string {:readers *data-readers*} content)] ; Use *data-readers* for safety
            (when (map? data)
              (println (format "        Found ns compile to compile: %s" (get data :ns-compile)))
              (get data :ns-compile))))))
    (catch Exception e
      (println "WARN: Could not read META-INF/build.edn from" jar-path ":" (.getMessage e))
      nil)))

(defn uber
  "Build the UberJAR file."
  [opts]
  (let [{:keys [class-dir
                basis
                lib
                version
                src-dirs
                resource-dirs
                uber-file
                ns-compile
                main] :as merged-opts}
        (merge (build-opts opts) opts)]
    (println "Building UberJAR:" uber-file)
    (clean merged-opts) ; Pass merged opts to clean
    (b/copy-dir {:src-dirs (concat src-dirs resource-dirs)
                 :target-dir class-dir})

    (let [classpath-paths (keys (:classpath basis))
          classpath-jars (filter #(.endsWith % ".jar") classpath-paths)
          compile-namespaces (->> classpath-jars
                                  (keep find-compile-ns-in-jar)
                                  (apply concat)
                                  (distinct)
                                  (vec))

          _
          (print "Praparing ns for compile: " ns-compile)
          
          compile-namespaces 
          (conj compile-namespaces (symbol ns-compile))]

      (if (seq compile-namespaces)
        (println "Found namespaces to compile from dependencies:" compile-namespaces)
        (println "No :ns-compile directives found in dependency META-INF/build.edn files."))

      (println "Writing pom file")
      (b/write-pom {:class-dir class-dir
                    :lib lib
                    :version version
                    :basis basis
                    :src-dirs src-dirs})


      (println "Compiling")
      (b/compile-clj {:basis basis
                      :src-dirs src-dirs
                      :class-dir class-dir
                      :ns-compile compile-namespaces}))

    (b/uber {:class-dir class-dir
             :uber-file uber-file
             :basis basis
             :main main})))
